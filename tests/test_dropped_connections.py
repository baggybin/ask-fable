"""A body cut short mid-read (http.client.IncompleteRead) is an unreachable host, not a
crash: urllib only wraps what sending a request raises, never what read() raises."""

from __future__ import annotations

import http.client

import ask_fable.ali as ali
import ask_fable.codeindex as ci
import ask_fable.controlpage as controlpage


class _CutBody:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *_a):
        raise http.client.IncompleteRead(b"{\"par", 100)


class _Opener:
    def open(self, *_a, **_k):
        return _CutBody()


def test_ali_catalog_reports_a_cut_body(monkeypatch):
    monkeypatch.setattr(ali.urllib.request, "urlopen", lambda *a, **k: _CutBody())
    out = ali.catalog()
    assert out["cloud_ok"] is False and out["error"]


def test_codeindex_post_reports_a_cut_body(monkeypatch):
    monkeypatch.setattr(ci, "_get_opener", lambda: _Opener())
    obj, err = ci._post("http://embed.invalid/v1/embeddings", {"input": ["x"]}, 1.0)
    assert obj is None and err.startswith("network error")


def test_controlpage_status_reports_a_cut_body(monkeypatch):
    monkeypatch.setattr(controlpage, "_get_opener", lambda: _Opener())
    assert controlpage.status(timeout=1.0) is None
