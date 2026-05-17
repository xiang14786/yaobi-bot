"""
train_ml.py v4 — LightGBM + Optuna 自動調參
=============================================
改進點：
  1. 二元分類：漲>3% vs 其他
  2. Optuna 自動嘗試 80 種參數組合，找最佳
  3. 特徵工程（交叉特徵）
  4. LightGBM：速度更快、記憶體更省、葉節點生長更靈活
  5. 新增特徵：btc_change_24h、consistency_score

執行：
    pip install lightgbm optuna scikit-learn joblib pandas
    python train_ml.py

輸出：
    model_crypto.pkl / model_tw.pkl / model_us.pkl
"""

import sqlite3
import joblib
import warnings
import pandas as pd
import numpy as np
from pathlib import Path
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split
from sklearn.metrics import roc_auc_score, accuracy_score, classification_report
from lightgbm import LGBMClassifier
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
warnings.filterwarnings("ignore")

DB_PATH = Path(__file__).parent / "yaobi_fly.db"

FEATURES = [
    "total_score", "early_score", "confidence",
    "change_pct", "feat1", "feat2", "feat3", "feat4",
    "btc_change_24h", "consistency_score",  # 市場環境 + 指標一致性
    "dow", "rank_in_session"                # 時間週期性 + 同批相對排名
]

MARKET_LABELS = {
    "crypto": {"feat1": "funding_rate", "feat2": "long_short",   "feat3": "top_trader", "feat4": "oi_change"},
    "tw":     {"feat1": "foreign_net億", "feat2": "foreign_streak", "feat3": "score_bb", "feat4": "margin_chg"},
    "us":     {"feat1": "rs_rating",    "feat2": "accum_score",  "feat3": "momentum",   "feat4": "inst_pct"},
}


def load_data(market: str) -> pd.DataFrame:
    conn = sqlite3.connect(DB_PATH)
    df = pd.read_sql_query(
        "SELECT * FROM ml_scan_data WHERE market=? AND outcome_label IS NOT NULL",
        conn, params=(market,)
    )
    conn.close()
    return df


def add_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["score_x_conf"] = df["total_score"] * df["confidence"]
    df["early_ratio"]  = df["early_score"] / (df["total_score"] + 1)
    df["change_sq"]    = df["change_pct"].abs() ** 1.5
    df["feat1x2"]      = df["feat1"] * df["feat2"]
    df["feat2x3"]      = df["feat2"] * df["feat3"]
    return df


ALL_FEATURES = FEATURES + ["score_x_conf", "early_ratio", "change_sq", "feat1x2", "feat2x3"]


def train_market(market: str, n_trials: int = 80):
    print(f"\n{'='*48}")
    print(f"  市場：{market.upper()}  (Optuna {n_trials} 試)")
    print(f"{'='*48}")

    df = load_data(market)
    if len(df) < 100:
        print(f"  [跳過] 資料不足 ({len(df)} 筆)")
        return None

    df = add_features(df)
    df["label"] = (df["outcome_label"] == 1).astype(int)

    # 舊資料可能缺欄位，補預設值
    for col in ["btc_change_24h", "consistency_score"]:
        if col not in df.columns:
            df[col] = 0
    for col in ["dow", "rank_in_session"]:
        if col not in df.columns:
            df[col] = -1

    pos_rate = df["label"].mean()
    print(f"  資料: {len(df)} 筆  |  漲>3% 比例: {pos_rate:.1%}")

    X = df[ALL_FEATURES].fillna(0).values
    y = df["label"].values

    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    scale_pos = (y == 0).sum() / max(1, (y == 1).sum())

    # ── Optuna 自動調參 ──
    def objective(trial):
        params = {
            "n_estimators":       trial.suggest_int("n_estimators", 100, 600),
            "num_leaves":         trial.suggest_int("num_leaves", 15, 127),
            "max_depth":          trial.suggest_int("max_depth", 3, 12),
            "learning_rate":      trial.suggest_float("learning_rate", 0.01, 0.3, log=True),
            "subsample":          trial.suggest_float("subsample", 0.6, 1.0),
            "subsample_freq":     trial.suggest_int("subsample_freq", 1, 5),
            "colsample_bytree":   trial.suggest_float("colsample_bytree", 0.6, 1.0),
            "min_child_samples":  trial.suggest_int("min_child_samples", 5, 50),
            "reg_alpha":          trial.suggest_float("reg_alpha", 0, 2),
            "reg_lambda":         trial.suggest_float("reg_lambda", 0, 5),
            "scale_pos_weight":   scale_pos,
            "verbose":            -1,
            "random_state":       42,
            "n_jobs":             1,
        }
        model = LGBMClassifier(**params)
        scores = cross_val_score(model, X, y, cv=cv, scoring="roc_auc", n_jobs=1)
        return scores.mean()

    print(f"  Optuna 搜尋中...", end="", flush=True)
    study = optuna.create_study(direction="maximize",
                                 sampler=optuna.samplers.TPESampler(seed=42))
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    print(f" 完成！最佳 CV AUC: {study.best_value:.4f}")

    # 最佳參數訓練
    best_params = {**study.best_params,
                   "scale_pos_weight": scale_pos,
                   "verbose": -1,
                   "random_state": 42,
                   "n_jobs": 1}

    X_tr, X_te, y_tr, y_te = train_test_split(X, y, test_size=0.2,
                                                random_state=42, stratify=y)
    model = LGBMClassifier(**best_params)
    model.fit(X_tr, y_tr,
              eval_set=[(X_te, y_te)],
              callbacks=[])

    y_pred = model.predict(X_te)
    y_prob = model.predict_proba(X_te)[:, 1]
    acc    = accuracy_score(y_te, y_pred)
    auc    = roc_auc_score(y_te, y_prob)

    print(f"  測試集  ACC: {acc:.3f}  AUC: {auc:.3f}")
    print(f"\n  分類報告：")
    print(classification_report(y_te, y_pred,
                                 target_names=["其他", "漲>3%"],
                                 labels=[0, 1]))

    # 特徵重要性
    feat_labels = MARKET_LABELS.get(market, {})
    imps = sorted(zip(ALL_FEATURES, model.feature_importances_),
                  key=lambda x: x[1], reverse=True)
    print("  特徵重要性 Top 8：")
    for feat, imp in imps[:8]:
        label = feat_labels.get(feat, feat)
        bar   = "█" * int(imp / max(imps[0][1], 1) * 40)
        print(f"    {label:20s} {bar} {imp:.0f}")

    print(f"\n  最佳參數：")
    for k, v in study.best_params.items():
        print(f"    {k}: {v}")

    return model


def main():
    print("=== 妖幣雷達 ML 訓練 v4 (LightGBM + Optuna) ===")
    print(f"資料庫: {DB_PATH}")

    if not DB_PATH.exists():
        print(f"[ERROR] 找不到 {DB_PATH}，請先執行 import_ml_csv.py")
        return

    models = {}
    for market in ["crypto", "tw", "us"]:
        model = train_market(market, n_trials=80)
        if model:
            models[market] = model
            out = f"model_{market}.pkl"
            joblib.dump({"model": model, "features": ALL_FEATURES}, out)
            print(f"\n  [saved] {out}")

    print(f"\n{'='*48}")
    print(f"訓練完成！共 {len(models)} 個模型")
    print(f"\n上傳到 Fly.io：")
    print("  fly sftp shell -a yaobi-bot-tw")
    for m in models:
        print(f"  >> put model_{m}.pkl /data/model_{m}.pkl")
    print("  >> exit")


if __name__ == "__main__":
    main()
