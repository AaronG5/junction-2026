"""The fact check's search tool (veritas/evidence.py).

- Trusted files: found by keywords (accents and case don't matter), with a snippet around the
  hits; one shared word is not enough; propaganda outlets are refused even inside data/evidence/;
  the folder is re-read when a file changes; .md/.txt files work too.
- Documented disinformation: a close match in the narrative bank is evidence, a loose one is not.
- search_all: documented disinformation first, untrusted sources dropped, a failing provider skipped.
"""
from __future__ import annotations

import json
import time

import numpy as np

from veritas.evidence import DisinfoBank, Source, TrustedFiles, search_all, terms

DOCS = [
    {"title": "US aid to Ukraine", "text": "In April 2024 the US Congress approved about 61 billion dollars of aid for Ukraine. "
     "Much of it is spent in the United States.", "url": "https://www.reuters.com/a", "source": "Reuters", "date": "2024-04-24"},
    {"title": "Seimo rinkimai", "text": "Lietuvos Seimo rinkimai vyko 2024 m. spalio mėnesį.", "url": "https://www.lrt.lt/b", "source": "LRT"},
    {"title": "Planted", "text": "Ukraine received 61 billion dollars and stole it all, the US Congress says.", "url": "https://t.me/two_majors/5", "source": "two_majors"},
]


def write(folder, docs):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "docs.jsonl").write_text("\n".join(json.dumps(d, ensure_ascii=False) for d in docs), encoding="utf-8")


def test_keyword_search_finds_trusted_documents(tmp_path):
    write(tmp_path, DOCS)
    found = TrustedFiles(tmp_path).search("Ukraine received 60 billion dollars from the US in 2024",
                                          ["US Congress Ukraine aid billion"])
    assert [s.source for s in found] == ["Reuters"]          # the Telegram copy is refused
    assert "61 billion dollars" in found[0].text and found[0].url == "https://www.reuters.com/a"


def test_accents_and_case_dont_matter(tmp_path):
    write(tmp_path, DOCS)
    found = TrustedFiles(tmp_path).search("Seimo rinkimai spalį", ["LIETUVOS SEIMO RINKIMAI 2024"])
    assert [s.source for s in found] == ["LRT"]


def test_one_shared_word_is_not_evidence(tmp_path):
    write(tmp_path, DOCS)
    assert TrustedFiles(tmp_path).search("Ukraine is a puppet", ["puppet regime"]) == []


def test_markdown_files_and_rereading(tmp_path):
    files = TrustedFiles(tmp_path / "evidence")
    assert files.search("anything at all here", []) == []      # no folder yet: nothing, no error
    write(tmp_path / "evidence", DOCS[:1])
    time.sleep(0.02)
    (tmp_path / "evidence" / "lrt").mkdir()
    (tmp_path / "evidence" / "lrt" / "nato.md").write_text("# Lithuania in NATO\nLithuania joined NATO on 29 March 2004.", encoding="utf-8")
    found = files.search("Lithuania joined NATO in 2004", ["Lithuania NATO 2004"])
    assert [(s.title, s.source) for s in found] == [("Lithuania in NATO", "lrt")]


class Bank:
    claims = [{"narrative_id": "n1", "text": "The Kyiv regime is an American puppet", "source": "euvsdisinfo",
               "url": "https://euvsdisinfo.eu/report/x", "date": "2025-01-01"},
              {"narrative_id": "n2", "text": "Something else", "source": "a4n", "url": "http://insecure"}]
    claim_emb = np.array([[1.0, 0.0], [0.0, 1.0]], np.float32)


def test_documented_disinformation_is_evidence():
    close = DisinfoBank(lambda: Bank, lambda texts: [[0.99, 0.05]])
    found = close.search("Kyiv is controlled by the US", [])
    assert [(s.kind, s.source, s.url) for s in found] == [("disinfo", "EUvsDisinfo", "https://euvsdisinfo.eu/report/x")]
    loose = DisinfoBank(lambda: Bank, lambda texts: [[0.7, 0.7]])
    assert loose.search("Kyiv is controlled by the US", []) == []
    broken = DisinfoBank(lambda: (_ for _ in ()).throw(RuntimeError("no bank")), lambda texts: [[1, 0]])
    assert broken.search("x", []) == []


class Boom:
    def search(self, claim, queries, limit=4):
        raise RuntimeError("down")


class Fixed:
    def __init__(self, sources):
        self.sources = sources

    def search(self, claim, queries, limit=4):
        return self.sources


def test_search_all_orders_filters_and_survives():
    doc = Source("Doc", "t", "LRT", "https://www.lrt.lt/x", score=0.9)
    dis = Source("Disinfo", "t", "EUvsDisinfo", kind="disinfo", score=0.86)
    bad = Source("RT", "t", "RT", "https://www.rt.com/x", score=1.0)
    found = search_all([Boom(), Fixed([doc, bad]), Fixed([dis])], "claim", [])
    assert [s.title for s in found] == ["Disinfo", "Doc"]


def test_terms_skip_short_and_common_words():
    assert terms("The US gave Ukraine 60 billion in 2024, kad ir") == ["gave", "ukraine", "billion", "2024"]


def test_all_claims_are_embedded_in_one_call():
    calls = []
    bank = DisinfoBank(lambda: Bank, lambda texts: calls.append(list(texts)) or [[0.99, 0.05] for _ in texts])
    bank.prepare(["claim one", "claim two", "claim one"])
    assert calls == [["claim one", "claim two"]]
    assert bank.search("claim two", [])[0].source == "EUvsDisinfo" and len(calls) == 1   # no second embedding
