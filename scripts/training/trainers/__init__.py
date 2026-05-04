"""External lane trainer wrappers for CRT pipeline.

Each trainer:
1. Reads CRT manifest JSON (schema: crt_model_manifest.v1)
2. Executes the real ML training in D:\ai
3. Exports artifacts to the target path
4. Writes a result.json that your_trainer.py ingests via --result-json-path
"""
