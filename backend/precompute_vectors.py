"""Write the embedding inputs of every post in data/parsed/ (no database needed), for computing the vectors
elsewhere, e.g. on a cloud GPU with the same EmbeddingGemma GGUF:

  uv run python backend/precompute_vectors.py inputs.jsonl      # {"key": vector_key, "input": exact model input}

The vectors then go into data/vectors/posts_768.npz (arrays "keys", "vectors"), which import_data.py uses.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from veritas import importer  # noqa: E402
from veritas.ai import document_input  # noqa: E402


def main(out: str) -> int:
    seen, n = set(), 0
    with open(out, "w", encoding="utf-8") as f:
        for name in importer.SOURCES:
            for post in importer.records(name):
                key = importer.vector_key(post.text, post.title)
                if key in seen:
                    continue
                seen.add(key)
                f.write(json.dumps({"key": key, "input": document_input(post.text, post.title)}, ensure_ascii=False) + "\n")
                n += 1
    print(f"{n} inputs -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "inputs.jsonl"))
