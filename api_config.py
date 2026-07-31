"""Credential-free configuration adapter for the public evaluation pipelines."""
from __future__ import annotations

import os


ANSWER_API_BASE = os.environ.get("ANSWER_API_BASE", "").rstrip("/")
ANSWER_API_KEY = os.environ.get("ANSWER_API_KEY", "")
ANSWER_MODEL = os.environ.get("ANSWER_MODEL", "")

JUDGE_API_BASE = os.environ.get("JUDGE_API_BASE", "").rstrip("/")
JUDGE_API_KEY = os.environ.get("JUDGE_API_KEY", "")
JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "")
JUDGE_VERSION = os.environ.get("JUDGE_VERSION", "")
