"""publikclip pipeline: long video in, scored vertical clips out.

Everything heavy runs locally. The only network calls are ingest downloads
and the 2-3 LLM calls in the scoring/music stages (BYO key or Ollama).
"""

__version__ = "0.2.0"
