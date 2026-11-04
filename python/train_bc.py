"""Train the numpy behaviour-cloning candidate scorer.

Usage:
    python godot_kernel/python/train_bc.py --data data/bc_v13_200.jsonl --epochs 40
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "python"))

from bc_model import (  # noqa: E402
    BCPolicyNet,
    CAND_DIM,
    GLOBAL_DIM,
    SKIP_SLOT,
    TOTAL_SLOTS,
    build_dataset,
    evaluate,
)


def train(
    records: list[dict],
    epochs: int,
    lr: float,
    seed: int,
) -> BCPolicyNet:
    from bc_model import MAX_CANDIDATES

    records = [record for record in records if int(record["action"]) < MAX_CANDIDATES]
    games = np.asarray([record["game"] for record in records])
    globals_, candidates, masks, embeds, chosen = build_dataset(records)
    split = int(games.max() * 0.8) if games.max() > 1 else -1
    train_rows = games <= split
    val_rows = ~train_rows
    model = BCPolicyNet(seed=seed)
    params = [model.embedding, model.w1, model.b1, model.w2, model.b2]
    first = [np.zeros_like(p) for p in params]
    second = [np.zeros_like(p) for p in params]
    beta1, beta2, eps = 0.9, 0.999, 1e-8
    step = 0
    for epoch in range(1, epochs + 1):
        probabilities, hidden, flat, _ = model.forward(
            globals_[train_rows], candidates[train_rows], embeds[train_rows], masks[train_rows]
        )
        rows = int(train_rows.sum())
        loss = -np.log(np.clip(probabilities[np.arange(rows), chosen[train_rows]], 1e-9, None)).mean()
        grad_scores = probabilities.copy()
        grad_scores[np.arange(rows), chosen[train_rows]] -= 1.0
        grad_scores /= rows
        flat_scores = grad_scores.reshape(-1)
        grad_w2 = hidden.T @ flat_scores[:, None]
        grad_b2 = np.asarray([flat_scores.sum()], dtype=np.float32)
        grad_hidden = (flat_scores[:, None] @ model.w2.T).reshape(hidden.shape)
        grad_pre = grad_hidden * (hidden > 0)
        grad_w1 = flat.T @ grad_pre
        grad_b1 = grad_pre.sum(axis=0)
        grad_flat = grad_pre @ model.w1.T
        grad_candidates = grad_flat[:, GLOBAL_DIM:].reshape(rows, TOTAL_SLOTS, CAND_DIM)
        grad_embedding = np.zeros_like(model.embedding)
        np.add.at(
            grad_embedding,
            embeds[train_rows].reshape(-1),
            grad_candidates[..., : model.embedding.shape[1]].reshape(-1, model.embedding.shape[1]),
        )
        step += 1
        grads = [grad_embedding, grad_w1, grad_b1, grad_w2, grad_b2]
        for index, (param, grad) in enumerate(zip(params, grads)):
            first[index] = beta1 * first[index] + (1 - beta1) * grad
            second[index] = beta2 * second[index] + (1 - beta2) * (grad * grad)
            m_hat = first[index] / (1 - beta1 ** step)
            v_hat = second[index] / (1 - beta2 ** step)
            param -= (lr * m_hat / (np.sqrt(v_hat) + eps)).astype(np.float32)
        if epoch == 1 or epoch % 5 == 0 or epoch == epochs:
            train_acc, train_skip = evaluate(
                model, globals_[train_rows], candidates[train_rows],
                masks[train_rows], embeds[train_rows], chosen[train_rows]
            )
            val_acc, val_skip = evaluate(
                model, globals_[val_rows], candidates[val_rows],
                masks[val_rows], embeds[val_rows], chosen[val_rows]
            )
            print(
                f"epoch={epoch} loss={loss:.4f} train_acc={train_acc:.3f} "
                f"train_skip={train_skip:.3f} val_acc={val_acc:.3f} val_skip={val_skip:.3f}"
            )
    return model


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=str, default=str(ROOT / "data" / "bc_v13_200.jsonl"))
    parser.add_argument("--out", type=str, default=str(ROOT / "data" / "bc_model.npz"))
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--lr", type=float, default=3e-3)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    records = [json.loads(line) for line in Path(args.data).open(encoding="utf-8")]
    model = train(records, args.epochs, args.lr, args.seed)
    model.save(Path(args.out))
    print(f"saved {args.out} samples={len(records)}")


if __name__ == "__main__":
    main()
