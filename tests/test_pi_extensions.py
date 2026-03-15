from pathlib import Path

import pytest


@pytest.mark.unit
class TestPiExtensionsExist:
    """Verify dgov pi extensions are present and well-formed."""

    def _ext_dir(self) -> Path:
        return Path(__file__).parent.parent / "src" / "dgov" / "pi-extensions"

    def test_extensions_directory_exists(self) -> None:
        assert self._ext_dir().is_dir()

    def test_progress_extension_exists(self) -> None:
        assert (self._ext_dir() / "dgov-progress.ts").is_file()

    def test_done_signal_extension_exists(self) -> None:
        assert (self._ext_dir() / "dgov-done-signal.ts").is_file()

    def test_context_extension_exists(self) -> None:
        assert (self._ext_dir() / "dgov-context.ts").is_file()

    def test_protected_paths_extension_exists(self) -> None:
        assert (self._ext_dir() / "dgov-protected-paths.ts").is_file()

    def test_extensions_have_default_export(self) -> None:
        """Each extension must export default function."""
        for ts_file in self._ext_dir().glob("*.ts"):
            content = ts_file.read_text()
            assert "export default function" in content, f"{ts_file.name} missing default export"

    def test_extensions_import_type(self) -> None:
        """Each extension must import ExtensionAPI type."""
        for ts_file in self._ext_dir().glob("*.ts"):
            content = ts_file.read_text()
            assert "ExtensionAPI" in content, f"{ts_file.name} missing ExtensionAPI import"
