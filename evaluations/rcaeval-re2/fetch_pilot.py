"""按固定版本下载并校验 RCAEval 首个试点案例。"""

import hashlib
import json
from pathlib import Path
from urllib.request import urlopen


def main() -> None:
    directory = Path(__file__).resolve().parent
    manifest = json.loads((directory / "pilot-manifest.json").read_text(encoding="utf-8"))
    target = directory.parents[1] / ".pytest_cache" / "rcaeval-re2" / manifest["case_id"]
    target.mkdir(parents=True, exist_ok=True)
    base = f"https://huggingface.co/datasets/{manifest['dataset']}/resolve/{manifest['revision']}"

    for item in manifest["files"]:
        path = target / Path(item["source"]).name
        size = item["bytes"]
        if path.is_file():
            with path.open("rb") as file:
                data = file.read(size + 1)
        else:
            with urlopen(f"{base}/{item['source']}", timeout=30) as response:
                data = response.read(size + 1)
        if (
            len(data) != size
            or hashlib.sha256(data).hexdigest() != item["sha256"]
            or not (data.startswith(b"PAR1") and data.endswith(b"PAR1"))
        ):
            raise ValueError(f"{path.name} 校验失败")
        if not path.exists():
            temporary = path.with_suffix(".partial")
            temporary.write_bytes(data)
            temporary.replace(path)
        print(f"{path.name}: {size} bytes, SHA-256 verified")


if __name__ == "__main__":
    main()
