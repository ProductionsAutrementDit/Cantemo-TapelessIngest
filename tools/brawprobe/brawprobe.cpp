// brawprobe — one Blackmagic RAW clip in, one JSON object out.
//
// The TapelessIngest `braw` provider runs this at SCAN time, once per clip,
// and stores what it prints: a card that has been archived can never be
// probed again, so everything the SDK will say about a clip is captured
// here, once.
//
//   brawprobe [--sdk-dir DIR] [--] <clip.braw>
//
// stdout: ONE JSON object, nothing else.
//   * the technical fields of PAD Forge's `brawdump probe` (geometry,
//     frame count, snapped frame rate and sensor rate, `offspeed`, frame-0
//     timecode, gamma/gamut, audio), computed the same way: `snapRate` and
//     the timecode normalisation are copied from brawdump, and the sensor
//     rate falls back over frames 0..min(8, frame_count)-1;
//   * `"metadata": {"clip": {...}, "frame0": {...}}` — EVERY key of the
//     clip's metadata iterator and of frame 0's, typed (numbers stay
//     numbers, strings stay strings, SafeArrays become JSON arrays).
//     `post_3dlut_*_data` (the embedded 3D LUT itself, ~430 000 bytes) is
//     the one family left out; its name is listed under `"excluded"`.
//
// stderr: diagnostics, `brawprobe: key=value ...`.
//
// Exit codes, brawdump's: 0 ok, 2 usage, 3 SDK init (no library could be
// loaded or no codec created), 4 the SDK refused the clip (open, geometry,
// or no sensor rate readable on frames 0..7), 5 a failure of our own (the
// JSON could not be written).
//
// One source for Linux and macOS. The SDK's strings are `const char*` on
// Linux and `CFStringRef` on macOS; the small shim below is the only place
// that knows. Out-strings the SDK hands back (camera type, timecode) are
// deliberately NOT freed: their ownership differs per platform, this
// process lives for one clip, and a leak of a few bytes is a better failure
// than a double free.
//
// The SDK itself is never vendored: the Makefile compiles the headers and
// BlackmagicRawAPIDispatch.cpp in place from SDK_DIR, and the library is
// loaded at run time from SDK_DIR/Libraries (or --sdk-dir).

#include <algorithm>
#include <chrono>
#include <cmath>
#include <condition_variable>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <vector>

#include "BlackmagicRawAPI.h"

#if defined(__APPLE__)
#include <CoreFoundation/CoreFoundation.h>
#endif

#ifndef BRAWPROBE_LIBRARY_DIR
#define BRAWPROBE_LIBRARY_DIR "/usr/lib64/blackmagic/BlackmagicRAWSDK/Linux/Libraries"
#endif

static const char* const kVersion = "1";

enum ExitCode {
    EXIT_OK        = 0,
    EXIT_USAGE     = 2,
    EXIT_SDK_INIT  = 3,
    EXIT_CLIP_LOAD = 4,
    EXIT_OUTPUT    = 5,
};

// ---------------------------------------------------------------------------
// The string shim: the only platform-specific code in this file.
// ---------------------------------------------------------------------------
#if defined(__APPLE__)
typedef CFStringRef NativeString;

static std::string toStd(NativeString s) {
    if (!s) return std::string();
    const CFIndex length = CFStringGetLength(s);
    const CFIndex size = CFStringGetMaximumSizeForEncoding(length, kCFStringEncodingUTF8) + 1;
    std::vector<char> buf((size_t)size, '\0');
    if (!CFStringGetCString(s, buf.data(), size, kCFStringEncodingUTF8)) return std::string();
    return std::string(buf.data());
}

// A path or directory handed TO the SDK.
struct InString {
    explicit InString(const std::string& s)
        : ref(CFStringCreateWithCString(kCFAllocatorDefault, s.c_str(), kCFStringEncodingUTF8)) {}
    ~InString() { if (ref) CFRelease(ref); }
    InString(const InString&) = delete;
    InString& operator=(const InString&) = delete;
    NativeString ref;
};
#else
typedef const char* NativeString;

static std::string toStd(NativeString s) { return s ? std::string(s) : std::string(); }

struct InString {
    explicit InString(const std::string& s) : keep(s), ref(keep.c_str()) {}
    std::string keep;
    NativeString ref;
};
#endif

// ---------------------------------------------------------------------------
// Diagnostics and JSON
// ---------------------------------------------------------------------------
static void diag(const char* fmt, ...) __attribute__((format(printf, 1, 2)));
static void diag(const char* fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    std::fputs("brawprobe: ", stderr);
    std::vfprintf(stderr, fmt, ap);
    std::fputc('\n', stderr);
    va_end(ap);
    std::fflush(stderr);
}

static std::string jsonString(const std::string& in) {
    std::string out = "\"";
    out.reserve(in.size() + 8);
    for (unsigned char c : in) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (c < 0x20) { char b[8]; std::snprintf(b, sizeof b, "\\u%04x", c); out += b; }
                else out += (char)c;
        }
    }
    out += "\"";
    return out;
}

static std::string jsonDouble(double v, const char* fmt) {
    if (!std::isfinite(v)) return "null";
    char b[64];
    std::snprintf(b, sizeof b, fmt, v);
    return b;
}

static std::string jsonInt(long long v) { return std::to_string(v); }
static std::string jsonUInt(unsigned long long v) { return std::to_string(v); }

static std::string basenameOf(const std::string& path) {
    const size_t slash = path.find_last_of('/');
    return slash == std::string::npos ? path : path.substr(slash + 1);
}

static const char* hresultText(HRESULT hr) {
    if (hr == S_OK)           return "ok";
    if (hr == S_FALSE)        return "false";
    if (hr == E_UNEXPECTED)   return "unexpected failure (E_UNEXPECTED)";
    if (hr == E_NOTIMPL)      return "not implemented (E_NOTIMPL)";
    if (hr == E_OUTOFMEMORY)  return "out of memory (E_OUTOFMEMORY)";
    if (hr == E_INVALIDARG)   return "invalid argument (E_INVALIDARG)";
    if (hr == E_NOINTERFACE)  return "no such interface (E_NOINTERFACE)";
    if (hr == E_POINTER)      return "invalid pointer (E_POINTER)";
    if (hr == E_FAIL)         return "unspecified failure (E_FAIL)";
    return "unknown HRESULT";
}

// ---------------------------------------------------------------------------
// Rates and timecode: copied from brawdump (PAD Forge,
// agent-ffmpeg/brawdump/main.cpp) so both tools snap identically. The SDK
// answers floats with error (29.970032 for a 29.97 clip), so a rate is
// snapped to the nearest known rate within 0.01 fps, else to n/1000.
// ---------------------------------------------------------------------------
struct Rational {
    long long num = 0, den = 1;
    bool operator==(const Rational& o) const { return num * o.den == o.num * den; }
    bool operator!=(const Rational& o) const { return !(*this == o); }
    double value() const { return den ? (double)num / (double)den : 0.0; }
};

static long long gcdOf(long long a, long long b) {
    a = a < 0 ? -a : a;
    b = b < 0 ? -b : b;
    while (b) { const long long t = a % b; a = b; b = t; }
    return a;
}

// A rate llround cannot hold (or no rate at all) is refused: false.
static bool snapRate(double fps, Rational& out) {
    if (!std::isfinite(fps) || fps <= 0.0 || fps * 1000.0 > 9e15) return false;
    static const Rational kKnown[] = {
        {24000, 1001}, {24, 1}, {25, 1}, {30000, 1001}, {30, 1}, {48, 1},
        {50, 1}, {60000, 1001}, {60, 1}, {100, 1}, {120, 1},
    };
    const Rational* best = nullptr;
    double bestGap = 0.0;
    for (const auto& k : kKnown) {
        const double gap = std::fabs(fps - k.value());
        if (gap <= 0.01 && (!best || gap < bestGap)) { best = &k; bestGap = gap; }
    }
    if (best) { out = *best; return true; }
    Rational r;
    r.num = std::llround(fps * 1000.0);
    r.den = 1000;
    if (r.num <= 0) return false;
    const long long g = gcdOf(r.num, r.den);
    if (g > 1) { r.num /= g; r.den /= g; }
    out = r;
    return true;
}

static std::string normaliseTimecode(const std::string& tc) {
    if (tc.size() == 11 && tc[2] == '.' && tc[5] == '.' && tc[8] == '.') {
        std::string out = tc;
        out[2] = out[5] = out[8] = ':';
        return out;
    }
    return tc;
}

// ---------------------------------------------------------------------------
// Metadata: every key, typed
// ---------------------------------------------------------------------------
static bool isExcludedKey(const std::string& key) {
    // The embedded 3D LUT's DATA (an array of ~430 000 bytes). Its name,
    // title, size and mode are ordinary keys and are kept.
    static const std::string prefix = "post_3dlut_";
    static const std::string suffix = "_data";
    return key.size() >= prefix.size() + suffix.size() &&
           key.compare(0, prefix.size(), prefix) == 0 &&
           key.compare(key.size() - suffix.size(), suffix.size(), suffix) == 0;
}

static std::string arrayJson(SafeArray* array) {
    if (!array) return "null";
    void* data = nullptr;
    BlackmagicRawVariantType type = 0;
    long lower = 0, upper = -1;
    if (SafeArrayGetVartype(array, &type) != S_OK) return "null";
    if (SafeArrayGetLBound(array, 1, &lower) != S_OK) return "null";
    if (SafeArrayGetUBound(array, 1, &upper) != S_OK) return "null";
    if (SafeArrayAccessData(array, &data) != S_OK || !data) return "null";
    const long count = upper - lower + 1;
    std::string out = "[";
    for (long i = 0; i < count; ++i) {
        if (i) out += ",";
        switch (type) {
            case blackmagicRawVariantTypeU8:      out += jsonUInt(((const uint8_t*)data)[i]); break;
            case blackmagicRawVariantTypeS16:     out += jsonInt(((const int16_t*)data)[i]); break;
            case blackmagicRawVariantTypeU16:     out += jsonUInt(((const uint16_t*)data)[i]); break;
            case blackmagicRawVariantTypeS32:     out += jsonInt(((const int32_t*)data)[i]); break;
            case blackmagicRawVariantTypeU32:     out += jsonUInt(((const uint32_t*)data)[i]); break;
            case blackmagicRawVariantTypeFloat32: out += jsonDouble(((const float*)data)[i], "%.9g"); break;
            case blackmagicRawVariantTypeFloat64: out += jsonDouble(((const double*)data)[i], "%.17g"); break;
            default:                              out += "null"; break;
        }
    }
    SafeArrayUnaccessData(array);
    return out + "]";
}

static std::string variantJson(const Variant& v) {
    switch (v.vt) {
        case blackmagicRawVariantTypeEmpty:     return "null";
        case blackmagicRawVariantTypeU8:        return jsonUInt(v.uiVal & 0xffu);
        case blackmagicRawVariantTypeS16:       return jsonInt(v.iVal);
        case blackmagicRawVariantTypeU16:       return jsonUInt(v.uiVal);
        case blackmagicRawVariantTypeS32:       return jsonInt(v.intVal);
        case blackmagicRawVariantTypeU32:       return jsonUInt(v.uintVal);
        case blackmagicRawVariantTypeFloat32:   return jsonDouble(v.fltVal, "%.9g");
        case blackmagicRawVariantTypeFloat64:   return jsonDouble(v.dblVal, "%.17g");
        case blackmagicRawVariantTypeString:    return jsonString(toStd(v.bstrVal));
        case blackmagicRawVariantTypeSafeArray: return arrayJson(v.parray);
        default:                                return "null";
    }
}

// `{"key": value, ...}` for every key the iterator yields. Excluded keys go
// to `excluded` as `<scope>.<key>`.
static std::string metadataJson(IBlackmagicRawMetadataIterator* it, const char* scope,
                                std::vector<std::string>& excluded) {
    std::string out = "{";
    bool first = true;
    while (it) {
        NativeString key = nullptr;
        if (it->GetKey(&key) != S_OK || !key) break;
        const std::string name = toStd(key);
        if (isExcludedKey(name)) {
            excluded.push_back(std::string(scope) + "." + name);
        } else {
            Variant value;
            VariantInit(&value);
            const HRESULT hr = it->GetData(&value);
            if (!first) out += ",";
            first = false;
            out += "\n      " + jsonString(name) + ": " + (hr == S_OK ? variantJson(value) : "null");
            if (hr != S_OK) {
                diag("event=warning stage=metadata scope=%s key=\"%s\" hr=0x%08x message=\"%s\"",
                     scope, name.c_str(), (unsigned)hr, hresultText(hr));
            }
            VariantClear(&value);
        }
        if (it->Next() != S_OK) break;
    }
    return out + (first ? "}" : "\n    }");
}

// ---------------------------------------------------------------------------
// Frame reads: frame 0's sensor rate and metadata, with brawdump's fallback
// over frames 1..7 for the sensor rate alone.
// ---------------------------------------------------------------------------
struct ReadState {
    std::mutex              m;
    std::condition_variable cv;
    bool                    done = false;
    HRESULT                 hr = S_OK;
    float                   sensorRate = 0.0f;
    bool                    wantMetadata = false;
    bool                    gotMetadata = false;
    std::string             metadata;
    std::vector<std::string>* excluded = nullptr;
};

class Callback : public IBlackmagicRawCallback {
public:
    explicit Callback(ReadState* s) : s_(s) {}

    void ReadComplete(IBlackmagicRawJob* job, HRESULT result, IBlackmagicRawFrame* frame) override {
        float rate = 0.0f;
        HRESULT hr = result;
        std::string metadata;
        bool gotMetadata = false;
        if (SUCCEEDED(hr) && frame) {
            hr = frame->GetSensorRate(&rate);
            if (s_->wantMetadata) {
                IBlackmagicRawMetadataIterator* it = nullptr;
                if (SUCCEEDED(frame->GetMetadataIterator(&it)) && it) {
                    metadata = metadataJson(it, "frame0", *s_->excluded);
                    gotMetadata = true;
                    it->Release();
                }
            }
        } else if (SUCCEEDED(hr)) {
            hr = E_POINTER;
        }
        // Released HERE, before the caller's FlushJobs (brawdump 20-1).
        job->Release();
        std::lock_guard<std::mutex> l(s_->m);
        s_->hr = hr;
        s_->sensorRate = rate;
        if (gotMetadata) { s_->metadata = metadata; s_->gotMetadata = true; }
        s_->done = true;
        s_->cv.notify_all();
    }

    void ReadAudioComplete(IBlackmagicRawJob*, HRESULT, IBlackmagicRawAudioBuffer*) override {}
    void DecodeComplete(IBlackmagicRawJob*, HRESULT) override {}
    void ProcessComplete(IBlackmagicRawJob*, HRESULT, IBlackmagicRawProcessedImage*) override {}
    void TrimProgress(IBlackmagicRawJob*, float) override {}
    void TrimComplete(IBlackmagicRawJob*, HRESULT) override {}
    void SidecarMetadataParseWarning(IBlackmagicRawClip*, NativeString, uint32_t, NativeString) override {}
    void SidecarMetadataParseError(IBlackmagicRawClip*, NativeString, uint32_t, NativeString) override {}
    void PreparePipelineComplete(void*, HRESULT) override {}
    HRESULT QueryInterface(REFIID, LPVOID*) override { return E_NOINTERFACE; }
    ULONG AddRef() override { return 1; }
    ULONG Release() override { return 1; }

private:
    ReadState* s_;
};

// How long one frame read may take before the probe gives up.
static const int kReadTimeoutSeconds = 60;

// One frame read, waited for at most kReadTimeoutSeconds. Returns the
// read's HRESULT; `timedOut` says the wait ran out instead.
static HRESULT readFrame(IBlackmagicRawClip* clip, ReadState& state, uint64_t index, bool& timedOut) {
    timedOut = false;
    {
        std::lock_guard<std::mutex> l(state.m);
        state.done = false;
        state.hr = S_OK;
        state.sensorRate = 0.0f;
    }
    IBlackmagicRawJob* job = nullptr;
    HRESULT hr = clip->CreateJobReadFrame(index, &job);
    if (SUCCEEDED(hr)) hr = job->Submit();
    if (FAILED(hr)) {
        if (job) job->Release();
        return hr;
    }
    std::unique_lock<std::mutex> l(state.m);
    if (!state.cv.wait_for(l, std::chrono::seconds(kReadTimeoutSeconds), [&] { return state.done; })) {
        timedOut = true;
        return E_FAIL;
    }
    return state.hr;
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
static void usage(FILE* out) {
    std::fprintf(out,
        "usage: brawprobe [--sdk-dir DIR] [--] <clip.braw>\n"
        "\n"
        "Prints the clip as ONE JSON object on stdout: brawdump's probe fields\n"
        "plus every clip and frame-0 metadata key under \"metadata\".\n"
        "DIR holds the Blackmagic RAW library (default: %s).\n"
        "\n"
        "exit codes: 0 ok, 2 usage, 3 sdk init, 4 clip refused, 5 output\n",
        BRAWPROBE_LIBRARY_DIR);
}

int main(int argc, char** argv) {
    std::string clipPath;
    std::string libraryDir = BRAWPROBE_LIBRARY_DIR;
    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];
        if (a == "-h" || a == "--help") { usage(stdout); return EXIT_OK; }
        if (a == "--sdk-dir") {
            if (i + 1 >= argc || !*argv[i + 1]) { diag("event=error stage=usage message=\"--sdk-dir needs a directory\""); return EXIT_USAGE; }
            libraryDir = argv[++i];
        } else if (a == "--") {
            if (i + 2 != argc || !clipPath.empty()) { usage(stderr); return EXIT_USAGE; }
            clipPath = argv[++i];
        } else if (!a.empty() && a[0] == '-') {
            diag("event=error stage=usage message=\"unknown option '%s'\"", a.c_str());
            usage(stderr);
            return EXIT_USAGE;
        } else if (clipPath.empty()) {
            clipPath = a;
        } else {
            diag("event=error stage=usage message=\"one clip per run (got '%s' after '%s')\"", a.c_str(), clipPath.c_str());
            return EXIT_USAGE;
        }
    }
    if (clipPath.empty()) { usage(stderr); return EXIT_USAGE; }

    // --- SDK -----------------------------------------------------------------
    IBlackmagicRawFactory* factory = nullptr;
    {
        InString dir(libraryDir);
        factory = CreateBlackmagicRawFactoryInstanceFromPath(dir.ref);
    }
    if (!factory) {
        diag("event=error stage=sdk_init message=\"no Blackmagic RAW library could be loaded\" sdk_dir=\"%s\"", libraryDir.c_str());
        return EXIT_SDK_INIT;
    }
    IBlackmagicRaw* codec = nullptr;
    HRESULT hr = factory->CreateCodec(&codec);
    if (FAILED(hr) || !codec) {
        diag("event=error stage=sdk_init hr=0x%08x message=\"could not create a codec: %s\" sdk_dir=\"%s\"",
             (unsigned)hr, hresultText(hr), libraryDir.c_str());
        return EXIT_SDK_INIT;
    }

    // --- clip ----------------------------------------------------------------
    IBlackmagicRawClip* clip = nullptr;
    {
        InString path(clipPath);
        hr = path.ref ? codec->OpenClip(path.ref, &clip) : E_INVALIDARG;
    }
    if (FAILED(hr) || !clip) {
        diag("event=error stage=clip_load hr=0x%08x message=\"the SDK could not open the clip: %s\" clip=\"%s\"",
             (unsigned)(FAILED(hr) ? hr : E_POINTER), hresultText(FAILED(hr) ? hr : E_POINTER), clipPath.c_str());
        return EXIT_CLIP_LOAD;
    }

    uint32_t width = 0, height = 0;
    uint64_t frameCount = 0;
    float fpsReported = 0.0f;
    hr = clip->GetWidth(&width);
    if (SUCCEEDED(hr)) hr = clip->GetHeight(&height);
    if (SUCCEEDED(hr)) hr = clip->GetFrameCount(&frameCount);
    if (SUCCEEDED(hr)) hr = clip->GetFrameRate(&fpsReported);
    if (FAILED(hr) || width == 0 || height == 0 || frameCount == 0 ||
        !std::isfinite(fpsReported) || fpsReported <= 0.0f) {
        diag("event=error stage=clip_load hr=0x%08x message=\"the SDK answered a %ux%u clip of %llu frames at %f fps\" clip=\"%s\"",
             (unsigned)hr, width, height, (unsigned long long)frameCount, (double)fpsReported, clipPath.c_str());
        return EXIT_CLIP_LOAD;
    }
    Rational fps;
    if (!snapRate(fpsReported, fps)) {
        diag("event=error stage=clip_load message=\"the SDK's frame rate %f cannot be snapped\" clip=\"%s\"",
             (double)fpsReported, clipPath.c_str());
        return EXIT_CLIP_LOAD;
    }

    std::string camera, timecode, gammaRecorded, gamut;
    {
        NativeString s = nullptr;
        if (SUCCEEDED(clip->GetCameraType(&s)) && s) camera = toStd(s);
        s = nullptr;
        if (SUCCEEDED(clip->GetTimecodeForFrame(0, &s)) && s) timecode = normaliseTimecode(toStd(s));
    }
    {
        IBlackmagicRawClipProcessingAttributes* attrs = nullptr;
        if (SUCCEEDED(clip->QueryInterface(IID_IBlackmagicRawClipProcessingAttributes, (LPVOID*)&attrs)) && attrs) {
            Variant v;
            VariantInit(&v);
            if (SUCCEEDED(attrs->GetClipAttribute(blackmagicRawClipProcessingAttributeGamma, &v)) &&
                v.vt == blackmagicRawVariantTypeString) gammaRecorded = toStd(v.bstrVal);
            VariantClear(&v);
            VariantInit(&v);
            if (SUCCEEDED(attrs->GetClipAttribute(blackmagicRawClipProcessingAttributeGamut, &v)) &&
                v.vt == blackmagicRawVariantTypeString) gamut = toStd(v.bstrVal);
            VariantClear(&v);
            attrs->Release();
        }
    }

    std::vector<std::string> excluded;
    std::string clipMetadata = "{}";
    {
        IBlackmagicRawMetadataIterator* it = nullptr;
        if (SUCCEEDED(clip->GetMetadataIterator(&it)) && it) {
            clipMetadata = metadataJson(it, "clip", excluded);
            it->Release();
        } else {
            diag("event=warning stage=metadata scope=clip message=\"the clip has no metadata iterator\"");
        }
    }

    // --- frames: frame 0 first, then brawdump's fallback -----------------------
    // Heap-allocated and never freed: on a read timeout the SDK may still
    // call back into it while the process exits.
    ReadState& state = *new ReadState();
    state.excluded = &excluded;
    Callback& callback = *new Callback(&state);
    codec->SetCallback(&callback);

    float sensorReported = 0.0f;
    Rational sensor;
    bool sensorFound = false;
    HRESULT lastHr = E_FAIL;
    uint64_t lastFrame = 0;
    std::string frame0Metadata = "{}";
    bool frame0Readable = false;
    const uint64_t tries = std::min<uint64_t>(frameCount, 8);
    for (uint64_t frame = 0; frame < tries && !sensorFound; ++frame) {
        state.wantMetadata = (frame == 0);
        bool timedOut = false;
        const HRESULT rhr = readFrame(clip, state, frame, timedOut);
        if (timedOut) {
            // A job is still in flight: no FlushJobs (it could hang too),
            // no orderly teardown — the process ends here.
            diag("event=error stage=sensor_rate frame=%llu message=\"frame %llu was not read within %d s\" clip=\"%s\"",
                 (unsigned long long)frame, (unsigned long long)frame, kReadTimeoutSeconds, clipPath.c_str());
            std::fflush(stdout);
            std::_Exit(EXIT_CLIP_LOAD);
        }
        if (frame == 0) {
            frame0Readable = SUCCEEDED(rhr);
            if (state.gotMetadata) frame0Metadata = state.metadata;
            if (!frame0Readable) {
                diag("event=warning stage=frame0 hr=0x%08x message=\"frame 0 could not be read: %s; its metadata is absent\"",
                     (unsigned)rhr, hresultText(rhr));
            }
        }
        if (SUCCEEDED(rhr) && std::isfinite(state.sensorRate) && state.sensorRate > 0.0f &&
            snapRate(state.sensorRate, sensor)) {
            sensorReported = state.sensorRate;
            sensorFound = true;
        } else {
            lastHr = SUCCEEDED(rhr) ? E_FAIL : rhr;
            lastFrame = frame;
        }
    }
    codec->FlushJobs();
    codec->SetCallback(nullptr);
    if (!sensorFound) {
        diag("event=error stage=sensor_rate frame=%llu hr=0x%08x message=\"no sensor rate could be read from frames 0-%llu: %s\" clip=\"%s\"",
             (unsigned long long)lastFrame, (unsigned)lastHr, (unsigned long long)(tries - 1), hresultText(lastHr),
             clipPath.c_str());
        return EXIT_CLIP_LOAD;
    }
    const bool offspeed = sensor != fps;

    // --- audio (brawdump's readAudioInfo) ---------------------------------------
    uint32_t channels = 0, sampleRate = 0, bits = 0;
    uint64_t samples = 0;
    {
        IBlackmagicRawClipAudio* audio = nullptr;
        if (SUCCEEDED(clip->QueryInterface(IID_IBlackmagicRawClipAudio, (LPVOID*)&audio)) && audio) {
            HRESULT ahr = audio->GetAudioChannelCount(&channels);
            if (SUCCEEDED(ahr)) ahr = audio->GetAudioSampleRate(&sampleRate);
            if (SUCCEEDED(ahr)) ahr = audio->GetAudioBitDepth(&bits);
            if (SUCCEEDED(ahr)) ahr = audio->GetAudioSampleCount(&samples);
            audio->Release();
            if (FAILED(ahr)) {
                diag("event=warning stage=audio hr=0x%08x message=\"an audio getter failed: %s; no audio reported\"",
                     (unsigned)ahr, hresultText(ahr));
                channels = 0;
            }
        }
        // All four or nothing: partial audio is no audio.
        if (channels == 0 || samples == 0 || sampleRate == 0 || bits == 0) {
            channels = 0; samples = 0; sampleRate = 0; bits = 0;
        }
    }

    // --- output ----------------------------------------------------------------
    std::string json = "{\n";
    auto field = [&json](const char* key, const std::string& value) {
        json += "  ";
        json += jsonString(key);
        json += ": ";
        json += value;
        json += ",\n";
    };
    field("brawprobe_version", jsonString(kVersion));
    field("clip", jsonString(basenameOf(clipPath)));
    field("path", jsonString(clipPath));
    field("sdk_dir", jsonString(libraryDir));
    field("camera", jsonString(camera));
    field("width", jsonUInt(width));
    field("height", jsonUInt(height));
    field("frame_count", jsonUInt(frameCount));
    field("fps_num", jsonInt(fps.num));
    field("fps_den", jsonInt(fps.den));
    field("fps_reported", jsonDouble(fpsReported, "%.6f"));
    field("sensor_rate_num", jsonInt(sensor.num));
    field("sensor_rate_den", jsonInt(sensor.den));
    field("sensor_rate_reported", jsonDouble(sensorReported, "%.6f"));
    field("offspeed", offspeed ? "true" : "false");
    field("timecode", jsonString(timecode));
    field("gamma_recorded", jsonString(gammaRecorded));
    field("gamut", jsonString(gamut));
    field("audio_channels", jsonUInt(channels));
    field("audio_sample_rate", jsonUInt(sampleRate));
    field("audio_bits", jsonUInt(bits));
    field("audio_samples", jsonUInt(samples));
    field("frame0_readable", frame0Readable ? "true" : "false");
    {
        std::string list = "[";
        for (size_t i = 0; i < excluded.size(); ++i) {
            if (i) list += ", ";
            list += jsonString(excluded[i]);
        }
        field("excluded", list + "]");
    }
    json += "  \"metadata\": {\n    \"clip\": " + clipMetadata + ",\n    \"frame0\": " + frame0Metadata + "\n  }\n}\n";

    clip->Release();
    codec->Release();
    factory->Release();

    if (std::fwrite(json.data(), 1, json.size(), stdout) != json.size() || std::fflush(stdout) != 0) {
        diag("event=error stage=output message=\"stdout could not be written\"");
        return EXIT_OUTPUT;
    }
    return EXIT_OK;
}
