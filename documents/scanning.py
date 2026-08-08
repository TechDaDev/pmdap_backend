from dataclasses import dataclass
from typing import Protocol

from documents.models import StoredFile


@dataclass(frozen=True)
class ScanResult:
    status: str
    detail: str


class FileSecurityScanner(Protocol):
    def scan(self, content: bytes) -> ScanResult: ...


class NotConfiguredFileSecurityScanner:
    def scan(self, content: bytes) -> ScanResult:
        del content
        return ScanResult(
            status=StoredFile.MalwareScanStatus.NOT_CONFIGURED,
            detail="Malware scanning is not configured.",
        )


default_file_security_scanner: FileSecurityScanner = NotConfiguredFileSecurityScanner()
