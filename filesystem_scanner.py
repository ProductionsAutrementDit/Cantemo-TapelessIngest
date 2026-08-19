# coding: utf-8

import logging
import os
import re
from typing import List, Tuple, Generator, Dict

log = logging.getLogger(__name__)


class ProviderPathMatcher:
    """
    Handles progressive path matching for a provider using segment-based matching.

    Each segment has:
    - pattern: regex to match directory name
    - required: whether this segment must be matched
    """

    def __init__(self, provider, segments: List[Dict]):
        """
        Initialize path matcher for a provider with segment list.

        Args:
            provider: Provider instance
            segments: List of {'pattern': str, 'required': bool}
        """
        self.provider = provider
        self.segments = segments
        self.current_segment_index = 0
        self.fully_matched = len(segments) == 0  # Empty segments = scan base folder

    def try_match_segment(self, dirname: str) -> bool:
        """
        Try to match current directory name against current segment.

        Returns True if:
        - We've already matched all segments (fully_matched)
        - Directory matches current segment pattern
        - Current segment is optional and we should try next segments

        Args:
            dirname: Directory name to match

        Returns:
            True if this matcher should continue into this directory
        """
        # Already fully matched all required segments
        if self.fully_matched:
            return True

        # No more segments to match
        if self.current_segment_index >= len(self.segments):
            self.fully_matched = True
            return True

        # Get current segment
        current_segment = self.segments[self.current_segment_index]
        pattern = current_segment['pattern']
        required = current_segment['required']

        # Try to match pattern
        regex = self._compile_pattern(pattern)
        matches = bool(regex.match(dirname))

        if matches:
            # Advance to next segment
            self.current_segment_index += 1

            # Check if we've matched all segments
            if self.current_segment_index >= len(self.segments):
                self.fully_matched = True

            return True

        # Doesn't match - check if current segment is optional
        if not required:
            # Skip this optional segment and try next one
            return self._try_skip_optional_and_match(dirname)

        # Required segment didn't match
        return False

    def _try_skip_optional_and_match(self, dirname: str) -> bool:
        """
        Skip current optional segment and try to match dirname against next segments.

        Args:
            dirname: Directory name to match

        Returns:
            True if any subsequent segment matches
        """
        # Save current position
        original_index = self.current_segment_index

        # Try skipping optional segments until we find a match or required segment
        while self.current_segment_index < len(self.segments):
            segment = self.segments[self.current_segment_index]

            # Skip this segment
            self.current_segment_index += 1

            # If we've exhausted all segments, we're fully matched
            if self.current_segment_index >= len(self.segments):
                self.fully_matched = True
                return True

            # Try to match against next segment
            next_segment = self.segments[self.current_segment_index]
            next_pattern = next_segment['pattern']
            next_regex = self._compile_pattern(next_pattern)

            if next_regex.match(dirname):
                # Match! Advance past this segment
                self.current_segment_index += 1
                if self.current_segment_index >= len(self.segments):
                    self.fully_matched = True
                return True

            # Didn't match - if next segment is required, we failed
            if next_segment['required']:
                # Restore original position and fail
                self.current_segment_index = original_index
                return False

            # Next segment is optional, continue trying to skip

        # Exhausted all segments without finding a match
        self.fully_matched = True
        return True

    def is_fully_matched(self) -> bool:
        """
        Check if all required segments have been matched.

        Returns:
            True if we can scan files in current directory
        """
        return self.fully_matched

    def clone(self):
        """Create a copy of this matcher for branching traversal."""
        new_matcher = ProviderPathMatcher(self.provider, self.segments)
        new_matcher.current_segment_index = self.current_segment_index
        new_matcher.fully_matched = self.fully_matched
        return new_matcher

    def _compile_pattern(self, pattern: str) -> re.Pattern:
        """
        Compile pattern string to regex.

        Args:
            pattern: Pattern string (e.g., "Clip", "(Clip|CLIP)", "[0-9]{4}")

        Returns:
            Compiled regex pattern
        """
        try:
            return re.compile(f"^{pattern}$")
        except re.error as e:
            log.warning(f"Invalid regex pattern '{pattern}': {e}")
            # Fallback to exact match
            return re.compile(f"^{re.escape(pattern)}$")


class FilesystemScanner:
    """
    Efficient filesystem scanner with provider-driven search strategy.

    Uses structured segment matching to progressively filter providers
    as we traverse the directory tree.
    """

    def scan_with_providers(
        self,
        base_path: str,
        providers: List,
        max_depth: int = 5,
    ) -> Generator[Tuple[str, str, List], None, None]:
        """
        Scan filesystem and yield files with their matching providers.

        Args:
            base_path: Root directory to scan
            providers: List of Provider instances
            max_depth: Maximum directory depth to traverse

        Yields:
            Tuple of (file_absolute_path, file_relative_path, [matching_providers])

        Behavior:
            - A single file can match MULTIPLE providers
            - Intelligently prunes search tree by progressively filtering providers
            - Handles optional segments (e.g., files copied without directory structure)
        """
        if not os.path.exists(base_path):
            log.warning(f"Base path does not exist: {base_path}")
            return

        # Build extension map for fast lookup
        extension_map = self._build_extension_map(providers)

        # Create matchers for all providers
        # Each provider can have multiple path variants
        all_matchers = []
        for provider in providers:
            path_segments_list = provider.getPathSegments()

            if not path_segments_list:
                # Provider has no path segments = scan base folder only
                matcher = ProviderPathMatcher(provider, [])
                all_matchers.append(matcher)
            else:
                # Provider has multiple path variants
                for segments in path_segments_list:
                    matcher = ProviderPathMatcher(provider, segments)
                    all_matchers.append(matcher)

        # Track processed files to avoid duplicates
        seen_files = set()

        # Recursively walk tree with progressive provider filtering
        for result in self._recursive_scan(
            base_path,
            base_path,
            all_matchers,
            extension_map,
            depth=0,
            max_depth=max_depth,
        ):
            file_path = result[0]
            if file_path not in seen_files:
                seen_files.add(file_path)
                yield result

    def _build_extension_map(self, providers: List) -> Dict[str, List]:
        """
        Build map of {extension: [providers]} for fast lookup.

        Returns:
            Dictionary mapping lowercase extensions to provider lists
        """
        extension_map = {}

        for provider in providers:
            for ext in provider.getExtensions():
                ext_lower = ext.lower()
                if ext_lower not in extension_map:
                    extension_map[ext_lower] = []
                extension_map[ext_lower].append(provider)

        return extension_map

    def _recursive_scan(
        self,
        current_path: str,
        base_path: str,
        provider_matchers: List[ProviderPathMatcher],
        extension_map: Dict[str, List],
        depth: int,
        max_depth: int,
    ) -> Generator[Tuple[str, str, List], None, None]:
        """
        Recursively scan directory tree with progressive provider filtering.

        Args:
            current_path: Current directory being scanned
            base_path: Base path for computing relative paths
            provider_matchers: List of ProviderPathMatcher instances still active
            extension_map: Extension to providers mapping
            depth: Current depth in tree
            max_depth: Maximum depth to traverse

        Yields:
            Tuple of (absolute_path, relative_path, [matching_providers])
        """
        if depth > max_depth:
            return

        # Get providers that have fully matched their patterns (can scan files here)
        fully_matched_providers = set()
        for matcher in provider_matchers:
            if matcher.is_fully_matched():
                fully_matched_providers.add(matcher.provider)

        # Scan files in current directory if any providers are fully matched
        if fully_matched_providers:
            for result in self._scan_directory(
                current_path, base_path, list(fully_matched_providers), extension_map
            ):
                yield result

        # Get subdirectories
        try:
            with os.scandir(current_path) as entries:
                subdirs = [e for e in entries if e.is_dir(follow_symlinks=False)]
        except PermissionError as e:
            log.warning(f"Permission denied scanning {current_path}: {e}")
            return
        except Exception as e:
            log.error(f"Error scanning directory {current_path}: {e}", exc_info=True)
            return

        # For each subdirectory, filter to matching providers and recurse
        for subdir in subdirs:
            dirname = subdir.name

            # Filter matchers that can match this subdirectory
            next_matchers = []

            for matcher in provider_matchers:
                # Clone matcher to try matching
                test_matcher = matcher.clone()

                if test_matcher.try_match_segment(dirname):
                    # This matcher can continue into this subdirectory
                    next_matchers.append(test_matcher)

            # Only recurse if at least one provider matches
            if next_matchers:
                subdir_path = subdir.path
                for result in self._recursive_scan(
                    subdir_path,
                    base_path,
                    next_matchers,
                    extension_map,
                    depth + 1,
                    max_depth,
                ):
                    yield result

    def _scan_directory(
        self,
        dir_path: str,
        base_path: str,
        providers: List,
        extension_map: Dict[str, List],
    ) -> Generator[Tuple[str, str, List], None, None]:
        """
        Scan a single directory (non-recursive) and yield matching files.

        Each file is checked against ALL provided providers - a single file
        can match multiple providers.

        Args:
            dir_path: Directory to scan
            base_path: Base path for computing relative paths
            providers: List of applicable providers
            extension_map: Extension to providers mapping

        Yields:
            Tuple of (absolute_path, relative_path, [matching_providers])
        """
        try:
            with os.scandir(dir_path) as entries:
                for entry in entries:
                    # Skip directories
                    if not entry.is_file(follow_symlinks=False):
                        continue

                    # Quick extension check
                    _, ext = os.path.splitext(entry.name)
                    ext_lower = ext.lower()

                    # Get providers that care about this extension
                    candidate_providers = extension_map.get(ext_lower, [])
                    if not candidate_providers:
                        continue

                    # Filter to only providers in our current list
                    # IMPORTANT: A file can match MULTIPLE providers
                    matching_providers = [
                        p for p in candidate_providers if p in providers
                    ]

                    if not matching_providers:
                        continue

                    # Compute paths
                    abs_path = entry.path
                    rel_path = os.path.relpath(abs_path, base_path)

                    yield (abs_path, rel_path, matching_providers)

        except PermissionError as e:
            log.warning(f"Permission denied scanning {dir_path}: {e}")
        except Exception as e:
            log.error(f"Error scanning directory {dir_path}: {e}", exc_info=True)
