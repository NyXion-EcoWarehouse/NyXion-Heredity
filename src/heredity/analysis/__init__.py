"""Bounded, offline artifact analysis; no target code is executed."""

from .deobfuscate import analyze_bytes, analyze_file

__all__ = ["analyze_bytes", "analyze_file"]
