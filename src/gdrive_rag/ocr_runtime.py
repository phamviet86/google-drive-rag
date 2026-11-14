"""Pinned local OCR runtime (PDFium + ONNX Runtime) installer, copied from local-rag-mcp
(Apache-2.0). The Docker image runs ``python -m gdrive_rag.ocr_runtime DIR`` at build time."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

PDFIUM_VERSION = "native-v7988"
ORT_VERSION = "1.27.0"
OCR_MODEL_REVISION = "oar-ocr-v0.7.0"
OCR_MODEL_FILES = (
    "pp-ocrv6_small_det.onnx",
    "pp-ocrv6_small_rec.onnx",
    "ppocrv6_dict.txt",
)
OCR_MODEL_BASE_URL = "https://github.com/GreatV/oar-ocr/releases/download/v0.7.0/"
# Pinned SHA-256 of each model file. They are fetched with Python (system CA store) because the
# extractor's own downloader fails TLS verification on some hosts (seen on linux/arm64).
OCR_MODEL_SHA256 = {
    "pp-ocrv6_small_det.onnx": "d73e0058b7a8086bbd57f3d10b8bcd4ff95363f67e06e2762b5e814fe9c9410e",
    "pp-ocrv6_small_rec.onnx": "5435fd747c9e0efe15a96d0b378d5bd157e9492ed8fd80edf08f30d02fa24634",
    "ppocrv6_dict.txt": "b5f2bfe2bdd9448429e3e82b51c789775d9b42f2403d082b00662eb77e401c5d",
}

ARTIFACTS: dict[tuple[str, str], dict[str, tuple[str, str]]] = {
    ("linux", "x86_64"): {
        "pdfium": (
            "https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-linux-x64.tgz",
            "6248189e07bbc33cdeb31976c539a88614307c8a19f3276dbd018efbe5b4a2a2",
        ),
        "ort": (
            "https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-linux-x64-1.27.0.tgz",
            "547e40a48f1fe73e3f812d7c88a948612c23f896b91e4e2ee1e232d7b468246f",
        ),
    },
    ("linux", "aarch64"): {
        "pdfium": (
            "https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-linux-arm64.tgz",
            "fb67e132abf9a816194bb40cbe1c9e1f6e3bfcaa0b0ef645bf2e6b9f155c8a73",
        ),
        "ort": (
            "https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-linux-aarch64-1.27.0.tgz",
            "3e4d83ac06924a32a07b6d7f91ce6f852876153fc0bbdf931bf517a140bfbe48",
        ),
    },
    ("darwin", "arm64"): {
        "pdfium": (
            "https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-mac-arm64.tgz",
            "4168356c2e62ad5e79553e2e9162f5c99949759d90cb83876a50311f0c32b9b3",
        ),
        "ort": (
            "https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-osx-arm64-1.27.0.tgz",
            "545e81c58152353acb0d1e8bd6ce4b62f830c0961f5b3acfedc790ffd76e477a",
        ),
    },
    ("windows", "amd64"): {
        "pdfium": (
            "https://github.com/firecrawl/pdfium-rs/releases/download/native-v7988/firecrawl-pdfium-win-x64.tgz",
            "6f398552d8021a89078f64466557251a204999177287b876be49877eb8750d50",
        ),
        "ort": (
            "https://github.com/microsoft/onnxruntime/releases/download/v1.27.0/onnxruntime-win-x64-1.27.0.zip",
            "c5c81710938e68079ff1a192b04897faabe4b43830d48f39f27ecd4e16138bfc",
        ),
    },
}


@dataclass(frozen=True)
class RuntimePaths:
    pdfium: Path
    ort: Path


class OCRRuntimeManager:
    def __init__(self, runtime_dir: Path, model_dir: Path):
        self.runtime_dir = runtime_dir
        self.model_dir = model_dir

    @staticmethod
    def platform_key() -> tuple[str, str]:
        system = platform.system().lower()
        machine = platform.machine().lower()
        aliases = {"amd64": "x86_64"} if system != "windows" else {"x86_64": "amd64"}
        return system, aliases.get(machine, machine)

    def install(self) -> dict[str, str]:
        key = self.platform_key()
        if key not in ARTIFACTS:
            raise RuntimeError(f"no pinned OCR runtime for {key[0]}/{key[1]}")
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        downloads = self.runtime_dir / "downloads"
        downloads.mkdir(exist_ok=True)
        for name, (url, expected) in ARTIFACTS[key].items():
            archive = downloads / url.rsplit("/", 1)[-1]
            if not archive.exists() or _sha256(archive) != expected:
                temporary = archive.with_suffix(archive.suffix + ".part")
                _download(url, temporary)
                if _sha256(temporary) != expected:
                    temporary.unlink(missing_ok=True)
                    raise RuntimeError(f"checksum mismatch for {archive.name}")
                temporary.replace(archive)
            destination = self.runtime_dir / name
            if destination.exists():
                shutil.rmtree(destination)
            destination.mkdir()
            _safe_extract(archive, destination)
        paths = self.paths()
        manifest = {
            "pdfium_version": PDFIUM_VERSION,
            "onnxruntime_version": ORT_VERSION,
            "ocr_model_revision": OCR_MODEL_REVISION,
            "pdfium": str(paths.pdfium),
            "ort": str(paths.ort),
        }
        (self.runtime_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        return manifest

    def provision_and_verify(self) -> dict[str, str]:
        """Install pinned native libraries and warm the pinned OCR model cache."""
        manifest = self.install()
        if not self.configure():
            raise RuntimeError("installed OCR runtime could not be configured")
        self.model_dir.mkdir(parents=True, exist_ok=True)
        self.fetch_models()
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(_smoke_pdf())
            import pdf_inspector

            resolved_model_dir = self.resolved_model_dir()
            if resolved_model_dir is None:
                raise RuntimeError("OCR model cache was not populated with the pinned model files")
            cached = pdf_inspector.process_pdf_with_ocr(
                str(temporary),
                mode="force",
                page_numbers=[1],
                model_directory=str(resolved_model_dir),
                offline=True,
            )
            if not cached.pages:
                raise RuntimeError("OCR model cache failed offline verification")
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        manifest["model_cache"] = str(resolved_model_dir)
        manifest["verified"] = "true"
        (self.runtime_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        return manifest

    def fetch_models(self) -> Path:
        """Download the pinned OCR model files into the model cache, verified by SHA-256."""
        target = self.model_dir / OCR_MODEL_REVISION
        target.mkdir(parents=True, exist_ok=True)
        for name, expected in OCR_MODEL_SHA256.items():
            destination = target / name
            if destination.is_file() and _sha256(destination) == expected:
                continue
            temporary = destination.with_suffix(destination.suffix + ".part")
            _download(OCR_MODEL_BASE_URL + name, temporary)
            if _sha256(temporary) != expected:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"checksum mismatch for {name}")
            temporary.replace(destination)
        return target

    def resolved_model_dir(self) -> Path | None:
        candidates = [self.model_dir]
        if self.model_dir.exists():
            candidates.extend(path.parent for path in self.model_dir.rglob(OCR_MODEL_FILES[0]))
        for candidate in candidates:
            if all((candidate / name).is_file() for name in OCR_MODEL_FILES):
                return candidate.resolve()
        return None

    def paths(self) -> RuntimePaths:
        pdfium_names = ("pdfium.dll", "libpdfium.dylib", "libpdfium.so")
        ort_names = ("onnxruntime.dll", "libonnxruntime.dylib", "libonnxruntime.so")
        pdfium = _find_library(self.runtime_dir / "pdfium", pdfium_names)
        ort = _find_library(self.runtime_dir / "ort", ort_names)
        return RuntimePaths(pdfium, ort)

    def configure(self) -> bool:
        try:
            paths = self.paths()
        except FileNotFoundError:
            return False
        os.environ["PDFIUM_LIB_PATH"] = str(paths.pdfium)
        os.environ["ORT_DYLIB_PATH"] = str(paths.ort)
        os.environ["PDF_INSPECTOR_MODEL_CACHE"] = str(self.model_dir)
        return True


DOWNLOAD_TIMEOUT_SECONDS = 60  # per socket read; a stalled build fails instead of hanging


def _download(url: str, destination: Path, timeout: float = DOWNLOAD_TIMEOUT_SECONDS) -> None:
    with urllib.request.urlopen(url, timeout=timeout) as response, destination.open("wb") as out:
        shutil.copyfileobj(response, out, 1024 * 1024)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as source:
            zip_members = source.infolist()
            if any(
                not _within(destination, destination / member.filename) for member in zip_members
            ):
                raise RuntimeError("unsafe path in OCR runtime archive")
            source.extractall(destination)
    else:
        with tarfile.open(archive, "r:gz") as source:
            tar_members = source.getmembers()
            if any(not _within(destination, destination / member.name) for member in tar_members):
                raise RuntimeError("unsafe path in OCR runtime archive")
            for member in tar_members:
                if member.issym():
                    link = (destination / member.name).parent / member.linkname
                    if not _within(destination, link):
                        raise RuntimeError("unsafe symbolic link in OCR runtime archive")
                elif member.islnk() and not _within(destination, destination / member.linkname):
                    raise RuntimeError("unsafe hard link in OCR runtime archive")
            source.extractall(destination, filter="data")


def _within(root: Path, path: Path) -> bool:
    resolved_root, resolved = root.resolve(), path.resolve()
    return resolved == resolved_root or resolved_root in resolved.parents


def _find_library(root: Path, names: tuple[str, ...]) -> Path:
    for name in names:
        matches = list(root.rglob(name)) if root.exists() else []
        if matches:
            return matches[0].resolve()
    raise FileNotFoundError(f"runtime library not found below {root}")


def _smoke_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>"
        ),
        b"<< /Length 41 >>\nstream\nBT /F1 18 Tf 20 100 Td (OCR check) Tj ET\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    payload = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(payload))
        payload.extend(f"{number} 0 obj\n".encode())
        payload.extend(obj + b"\nendobj\n")
    xref = len(payload)
    payload.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    payload.extend(b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:]))
    payload.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(payload)


def main(argv: list[str] | None = None) -> int:
    """Install and verify the runtime under DIR (``DIR/runtime`` and ``DIR/models``)."""
    import sys

    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m gdrive_rag.ocr_runtime DIR", file=sys.stderr)
        return 2
    root = Path(args[0])
    manifest = OCRRuntimeManager(root / "runtime", root / "models").provision_and_verify()
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
