# Build brawprobe against an INSTALLED Blackmagic RAW SDK.
#
# Nothing of the SDK enters this repository: the headers and
# BlackmagicRawAPIDispatch.cpp are compiled in place from SDK_DIR, and the
# library is loaded at run time from SDK_DIR/Libraries (baked in as the
# default, overridable with `brawprobe --sdk-dir DIR`).
#
#   make                                   # Linux, the RPM's SDK (the Portal server)
#   make SDK_DIR="/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac"   # macOS
#   make probe CLIP=/path/to/A001_C001.braw
#
# SDK_DIR is the platform folder holding Include/ and Libraries/. The macOS
# path contains spaces, so every path is only ever used inside quotes and
# `build` is phony (a prerequisite cannot contain a space): it always
# recompiles, two translation units, a few seconds.

HERE := $(shell cd "$$(dirname "$(strip $(MAKEFILE_LIST))")" && pwd)
UNAME := $(shell uname -s)

SDK_DIR ?= /usr/lib64/blackmagic/BlackmagicRAWSDK/Linux

SDK_INCLUDE   = $(SDK_DIR)/Include
SDK_DISPATCH  = $(SDK_INCLUDE)/BlackmagicRawAPIDispatch.cpp
SDK_LIBRARIES = $(SDK_DIR)/Libraries

BIN ?= $(HERE)/brawprobe

CXXFLAGS ?= -std=c++17 -O2 -Wall -Wextra -Wno-unused-parameter
# The dispatch file is Blackmagic's code, compiled as shipped.
DISPATCH_FLAGS ?= -std=c++17 -O2 -w

ifeq ($(UNAME),Darwin)
ifeq ($(origin CXX),default)
CXX := /usr/bin/clang++
endif
PLATFORM_FLAGS ?= -mmacosx-version-min=11.0
LDLIBS ?= -framework CoreFoundation
else
PLATFORM_FLAGS ?=
LDLIBS ?= -ldl -pthread
endif

.PHONY: all build clean guard-sdk probe where

all: build

where:
	@echo "SDK_DIR = $(SDK_DIR)"
	@echo "BIN     = $(BIN)"

guard-sdk:
	@test -f "$(SDK_INCLUDE)/BlackmagicRawAPI.h" -a -f "$(SDK_DISPATCH)" || { \
		echo "error: no Blackmagic RAW SDK found under SDK_DIR=$(SDK_DIR)"; \
		echo "       (looked for Include/BlackmagicRawAPI.h and Include/BlackmagicRawAPIDispatch.cpp)"; \
		echo "       Linux: install the Blackmagic RAW SDK RPM; macOS: pass"; \
		echo "       SDK_DIR=\"/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac\""; \
		exit 1; \
	}

build: guard-sdk
	@mkdir -p "$(HERE)/build"
	$(CXX) $(DISPATCH_FLAGS) $(PLATFORM_FLAGS) -I"$(SDK_INCLUDE)" -c "$(SDK_DISPATCH)" -o "$(HERE)/build/BlackmagicRawAPIDispatch.o"
	$(CXX) $(CXXFLAGS) $(PLATFORM_FLAGS) -I"$(SDK_INCLUDE)" '-DBRAWPROBE_LIBRARY_DIR="$(SDK_LIBRARIES)"' -c "$(HERE)/brawprobe.cpp" -o "$(HERE)/build/brawprobe.o"
	$(CXX) $(PLATFORM_FLAGS) "$(HERE)/build/brawprobe.o" "$(HERE)/build/BlackmagicRawAPIDispatch.o" $(LDLIBS) -o "$(BIN)"
	@echo "built $(BIN) (library dir: $(SDK_LIBRARIES))"

probe: build
	@test -n "$(CLIP)" || { echo 'usage: make probe CLIP=/path/to/clip.braw'; exit 1; }
	"$(BIN)" "$(CLIP)"

clean:
	rm -rf "$(HERE)/build" "$(BIN)"
