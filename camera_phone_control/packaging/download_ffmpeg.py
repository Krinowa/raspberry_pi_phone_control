"""Download and verify the pinned GitHub Releases FFmpeg archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

ALLOWED_HOSTS = frozenset({"github.com", "release-assets.githubusercontent.com"})
INITIAL_HOST = "github.com"
MAX_REDIRECTS = 3
MAX_DOWNLOAD_SIZE = 1024 * 1024 * 1024
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
URL_OVERRIDE = "PHONE_CONTROLLER_FFMPEG_URL"
SHA256_OVERRIDE = "PHONE_CONTROLLER_FFMPEG_SHA256"
_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


class FFmpegDownloadError(RuntimeError):
    pass


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        return None


def _validated_url(value, *, initial=False):
    if not isinstance(value, str) or not value or value != value.strip():
        raise FFmpegDownloadError("Approved FFmpeg URL is invalid.")
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").rstrip(".").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise FFmpegDownloadError("Approved FFmpeg URL has an invalid port.") from exc
    if parsed.scheme.lower() != "https":
        raise FFmpegDownloadError("Approved FFmpeg URL must use HTTPS.")
    if parsed.username is not None or parsed.password is not None:
        raise FFmpegDownloadError("Approved FFmpeg URL must not contain credentials.")
    if hostname not in ALLOWED_HOSTS:
        raise FFmpegDownloadError(
            f"Approved FFmpeg URL host {hostname or '<missing>'!r} is not allowed."
        )
    if port not in (None, 443):
        raise FFmpegDownloadError("Approved FFmpeg URL must use HTTPS port 443.")
    if not parsed.path.startswith("/"):
        raise FFmpegDownloadError("Approved FFmpeg URL must contain an absolute path.")
    if parsed.fragment:
        raise FFmpegDownloadError("Approved FFmpeg URL must not contain a fragment.")
    if initial:
        if hostname != INITIAL_HOST:
            raise FFmpegDownloadError("Approved FFmpeg URL must begin on github.com.")
        path_parts = parsed.path.split("/")
        if (
            len(path_parts) < 7
            or path_parts[3:5] != ["releases", "download"]
            or path_parts[5].lower() == "latest"
        ):
            raise FFmpegDownloadError(
                "Approved FFmpeg URL must identify a pinned GitHub release asset."
            )
    return value


def resolve_source_details(
    config_path, environment=None, *, allow_environment_override=False
):
    environment = os.environ if environment is None else environment
    try:
        config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FFmpegDownloadError("Pinned FFmpeg configuration is unreadable.") from exc
    if not isinstance(config, dict) or set(config) != {
        "schema_version",
        "release",
        "url",
        "sha256",
    }:
        raise FFmpegDownloadError("Pinned FFmpeg configuration has an invalid schema.")
    if (
        type(config["schema_version"]) is not int
        or config["schema_version"] != 1
        or not isinstance(config["release"], str)
        or not config["release"].strip()
    ):
        raise FFmpegDownloadError("Pinned FFmpeg configuration is unsupported.")

    override_url = ""
    override_sha256 = ""
    if allow_environment_override:
        override_url = environment.get(URL_OVERRIDE, "").strip()
        override_sha256 = environment.get(SHA256_OVERRIDE, "").strip()
        if bool(override_url) != bool(override_sha256):
            raise FFmpegDownloadError(
                "FFmpeg administrator overrides must provide both URL and SHA-256."
            )
    override_active = bool(override_url)
    url = override_url if override_active else config["url"]
    sha256 = override_sha256 if override_active else config["sha256"]
    _validated_url(url, initial=True)
    if not isinstance(sha256, str) or _SHA256.fullmatch(sha256) is None:
        raise FFmpegDownloadError(
            "Approved FFmpeg SHA-256 must be exactly 64 hexadecimal characters."
        )
    return {
        "url": url,
        "sha256": sha256.lower(),
        "source": "explicit environment override" if override_active else "pinned JSON",
        "override_active": override_active,
    }


def resolve_source(config_path, environment=None, *, allow_environment_override=False):
    details = resolve_source_details(
        config_path,
        environment,
        allow_environment_override=allow_environment_override,
    )
    return details["url"], details["sha256"]


def _safe_display_url(value):
    parsed = urlsplit(value)
    return parsed._replace(query="", fragment="").geturl()


def _request_once(opener, url, timeout):
    request = Request(
        url,
        headers={
            "Accept": "application/octet-stream",
            "User-Agent": "PhoneController-FFmpeg-Provisioner/1",
        },
        method="GET",
    )
    try:
        return opener.open(request, timeout=timeout)
    except HTTPError as exc:
        return exc
    except URLError as exc:
        raise FFmpegDownloadError(
            "Approved FFmpeg HTTPS request failed before receiving a response."
        ) from exc


def download_approved_ffmpeg(source_url, destination, expected_sha256, timeout=120):
    destination = Path(destination)
    if destination.exists() or destination.is_symlink():
        raise FFmpegDownloadError(
            "Approved FFmpeg download destination must not already exist."
        )
    current_url = _validated_url(source_url, initial=True)
    if (
        not isinstance(expected_sha256, str)
        or _SHA256.fullmatch(expected_sha256) is None
    ):
        raise FFmpegDownloadError(
            "Approved FFmpeg SHA-256 must be exactly 64 hexadecimal characters."
        )

    opener = build_opener(_NoRedirectHandler())
    redirects = 0
    try:
        while True:
            response = _request_once(opener, current_url, timeout)
            try:
                status = int(getattr(response, "status", response.getcode()))
                if status in REDIRECT_STATUSES:
                    location = response.headers.get("Location")
                    if not location:
                        raise FFmpegDownloadError(
                            "Approved FFmpeg redirect returned "
                            f"HTTP {status} without a Location header."
                        )
                    if redirects >= MAX_REDIRECTS:
                        raise FFmpegDownloadError(
                            f"Approved FFmpeg download exceeded {MAX_REDIRECTS} redirects."
                        )
                    current_url = _validated_url(urljoin(current_url, location))
                    redirects += 1
                    continue
                if status != 200:
                    raise FFmpegDownloadError(
                        f"Approved FFmpeg download returned HTTP {status}."
                    )

                content_length = response.headers.get("Content-Length")
                declared_size = None
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError as exc:
                        raise FFmpegDownloadError(
                            "Approved FFmpeg response has an invalid Content-Length."
                        ) from exc
                    if declared_size <= 0 or declared_size > MAX_DOWNLOAD_SIZE:
                        raise FFmpegDownloadError(
                            "Approved FFmpeg response size is invalid or exceeds 1 GiB."
                        )

                digest = hashlib.sha256()
                downloaded = 0
                with destination.open("xb") as output:
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_SIZE:
                            raise FFmpegDownloadError(
                                "Approved FFmpeg download exceeded 1 GiB."
                            )
                        digest.update(chunk)
                        output.write(chunk)
                if downloaded <= 0:
                    raise FFmpegDownloadError("Approved FFmpeg response was empty.")
                if declared_size is not None and downloaded != declared_size:
                    raise FFmpegDownloadError(
                        "Approved FFmpeg response did not match its Content-Length."
                    )
                actual_sha256 = digest.hexdigest()
                if actual_sha256 != expected_sha256.lower():
                    raise FFmpegDownloadError(
                        "Approved FFmpeg SHA-256 mismatch: "
                        f"expected {expected_sha256.lower()}, got {actual_sha256}."
                    )
                return destination
            finally:
                response.close()
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("destination", type=Path, nargs="?")
    parser.add_argument(
        "--allow-environment-override",
        action="store_true",
        help="explicitly allow paired PHONE_CONTROLLER_FFMPEG_* overrides",
    )
    parser.add_argument("--show-effective-source", action="store_true")
    args = parser.parse_args(argv)
    try:
        details = resolve_source_details(
            args.config,
            allow_environment_override=args.allow_environment_override,
        )
        if args.show_effective_source:
            print(f"FFmpeg configuration: {args.config.resolve()}")
            print(f"FFmpeg source: {details['source']}")
            print(f"FFmpeg override mode active: {details['override_active']}")
            print(f"FFmpeg release asset: {_safe_display_url(details['url'])}")
        if args.destination is None:
            if args.show_effective_source:
                return
            parser.error("destination is required unless --show-effective-source is used")
        downloaded = download_approved_ffmpeg(
            details["url"], args.destination, details["sha256"]
        )
    except FFmpegDownloadError as exc:
        parser.error(str(exc))
    print(f"Approved FFmpeg archive downloaded and verified: {downloaded.resolve()}")


if __name__ == "__main__":
    main()
