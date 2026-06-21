from __future__ import annotations

from pathlib import Path


def test_scanner_and_paper_do_not_use_float_conversions() -> None:
    for path in [Path("hyperliquid_trading_agent/app/hip4/scanner.py"), Path("hyperliquid_trading_agent/app/hip4/paper.py")]:
        source = path.read_text()
        assert "float(" not in source
        assert "Mapped[float" not in source
