from scripts.training.run_train_batch import render_command


def test_render_command_replaces_placeholders():
    job = {
        "manifest_path": "data/models/a.manifest.json",
        "model_id": "CRT.sur.chlg.g2026.1@feat-v9-institutional-1.0.0",
        "training_run_id": "run_seed_42__slice_a",
        "train_seed": 42,
        "dataset_slice_id": "slice_a",
    }
    cmd = render_command("python train.py --manifest {manifest_path} --seed {train_seed}", job)
    assert "--manifest data/models/a.manifest.json" in cmd
    assert "--seed 42" in cmd
