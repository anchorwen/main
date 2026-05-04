import json


class BrainRegistryLoader:
    def load_json(self, path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
