# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# analytics/at_risk_model.py
# Random Forest dropout risk classifier with feature engineering
# ==============================================================================

from __future__ import annotations

import pickle
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

warnings.filterwarnings("ignore")

from database.connection import get_session
from transformation.rules_engine import RiskRules, Thresholds
from utils.config import get_config
from utils.logger import log_step_failure, log_step_start, log_step_success, logger

_config = get_config()

try:
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import (
        accuracy_score, classification_report,
        f1_score, precision_score, recall_score, roc_auc_score,
    )
    from sklearn.model_selection import StratifiedKFold, cross_val_score
    from sklearn.preprocessing import LabelEncoder, StandardScaler
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    logger.warning("scikit-learn not installed — at-risk model disabled")


# ==============================================================================
# Result containers
# ==============================================================================

@dataclass
class ModelMetrics:
    """Classification model evaluation metrics."""
    accuracy:   float | None = None
    precision:  float | None = None
    recall:     float | None = None
    f1_score:   float | None = None
    roc_auc:    float | None = None
    cv_f1_mean: float | None = None   # Cross-validation mean F1
    cv_f1_std:  float | None = None   # Cross-validation std F1


@dataclass
class FeatureImportance:
    """Feature importance from the trained model."""
    feature:    str
    importance: float


@dataclass
class AtRiskRecord:
    """A single at-risk prediction for a program/year_level group."""
    program_code:       str
    program_name:       str
    college:            str
    academic_year:      str
    semester:           int
    year_level:         int
    total_enrolled:     int
    dropout_rate:       float | None
    risk_score:         float
    risk_label:         str            # Low | Moderate | High | Critical
    is_at_risk:         bool           # True = needs intervention
    top_risk_factors:   list[str] = field(default_factory=list)


@dataclass
class AtRiskReport:
    """Full at-risk model output."""
    generated_at:           str = ""
    rf_metrics:             ModelMetrics = field(default_factory=ModelMetrics)
    lr_metrics:             ModelMetrics = field(default_factory=ModelMetrics)
    feature_importances:    list[FeatureImportance] = field(default_factory=list)
    at_risk_records:        list[AtRiskRecord] = field(default_factory=list)
    high_risk_count:        int = 0
    critical_risk_count:    int = 0
    total_at_risk_enrolled: int = 0
    elapsed_seconds:        float = 0.0
    status:                 str = "pending"
    error_message:          str | None = None


# ==============================================================================
# At-risk model
# ==============================================================================

class AtRiskModel:
    """
    Classifies program/year_level groups as at-risk for high dropout using
    Random Forest as the primary model and Logistic Regression as baseline.

    Since the NEUST dataset is aggregated (no individual student records),
    the unit of prediction is a (program, year_level, semester) group.

    Features engineered:
        - year_level           — ordinal position in program
        - is_super_senior      — 1 if year_level >= 5
        - dropout_rate         — historical dropout rate for this group
        - shifter_out_rate     — shifters_out / total_enrolled
        - net_shifter_balance  — shifters_in - shifters_out
        - enrollment_size      — total_enrolled (log-scaled)
        - semester             — 1, 2, or 3
        - new_student_rate     — new_students / total_enrolled
        - returnee_rate        — returnees / total_enrolled

    Target variable:
        - is_high_dropout: 1 if dropout_rate >= 20% (configurable threshold)

    Training strategy:
        - Stratified K-Fold CV (k=5) to handle class imbalance
        - Train on all historical semesters except the latest (holdout)
        - Evaluate on latest semester

    Usage:
        model = AtRiskModel()
        report = model.run()
        model.print_summary(report)
    """

    DROPOUT_RISK_THRESHOLD = 20.0   # % — groups above this are labeled high dropout

    def __init__(self) -> None:
        self._started = time.monotonic()
        self._rf_model: RandomForestClassifier | None = None
        self._scaler:   StandardScaler | None = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> AtRiskReport:
        """Train the at-risk model and generate predictions."""
        report = AtRiskReport(generated_at=pd.Timestamp.now().isoformat())
        log_step_start(6, "At-risk model")

        if not SKLEARN_AVAILABLE:
            report.status        = "failed"
            report.error_message = "scikit-learn not installed"
            logger.error(report.error_message)
            return report

        try:
            # Load and engineer features
            df = self._load_feature_data()
            if df.empty or len(df) < 10:
                logger.warning(
                    "Insufficient data for at-risk model — need ≥10 rows. "
                    "Falling back to rules-based risk scoring only."
                )
                report = self._rules_based_fallback()
                return report

            logger.info("Feature data loaded — {} rows", len(df))

            # Engineer features and target
            X, y, feature_names = self._engineer_features(df)

            if y.sum() < 3:
                logger.warning(
                    "Too few positive samples ({}) for classifier — "
                    "falling back to rules-based scoring.",
                    int(y.sum()),
                )
                report = self._rules_based_fallback()
                return report

            # Split: all but latest semester = train, latest = test
            latest_sort_key = df["sort_key"].max()
            train_mask = df["sort_key"] < latest_sort_key
            test_mask  = df["sort_key"] == latest_sort_key

            X_train, y_train = X[train_mask], y[train_mask]
            X_test,  y_test  = X[test_mask],  y[test_mask]

            # Scale
            self._scaler = StandardScaler()
            X_train_s = self._scaler.fit_transform(X_train)
            X_test_s  = self._scaler.transform(X_test)

            # Train Random Forest
            logger.info("Training Random Forest ...")
            report.rf_metrics = self._train_random_forest(
                X_train_s, y_train, X_test_s, y_test, feature_names
            )

            # Train Logistic Regression (baseline)
            logger.info("Training Logistic Regression baseline ...")
            report.lr_metrics = self._train_logistic_regression(
                X_train_s, y_train, X_test_s, y_test
            )

            # Feature importances
            if self._rf_model is not None:
                report.feature_importances = self._get_feature_importances(feature_names)

            # Generate at-risk predictions for the latest semester
            report.at_risk_records = self._predict_at_risk(
                df[test_mask], X_test_s, feature_names
            )

            # Summary stats
            report.high_risk_count = sum(
                1 for r in report.at_risk_records
                if r.risk_label in ("High", "Critical")
            )
            report.critical_risk_count = sum(
                1 for r in report.at_risk_records if r.risk_label == "Critical"
            )
            report.total_at_risk_enrolled = sum(
                r.total_enrolled for r in report.at_risk_records if r.is_at_risk
            )

            # Save model
            self._save_model()

            report.status = "success"

        except Exception as exc:
            report.status        = "failed"
            report.error_message = str(exc)
            logger.exception("At-risk model failed: {}", exc)
            log_step_failure(6, "At-risk model", exc)

        finally:
            report.elapsed_seconds = time.monotonic() - self._started

        if report.status == "success":
            log_step_success(6, "At-risk model")
            logger.info(
                "At-risk model complete — high_risk={} | critical={} | "
                "at_risk_enrolled={:,} | {:.2f}s",
                report.high_risk_count,
                report.critical_risk_count,
                report.total_at_risk_enrolled,
                report.elapsed_seconds,
            )

        return report

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_feature_data(self) -> pd.DataFrame:
        """Load aggregated enrollment and outcome data from Gold for feature engineering."""
        with get_session() as session:
            rows = session.execute(
                text(
                    """
                    SELECT
                        dt.academic_year,
                        dt.semester,
                        dt.sort_key,
                        dp.program_code,
                        dp.program_name,
                        dp.college,
                        dyl.year_level,
                        dyl.is_irregular,
                        SUM(f.total_enrolled)       AS total_enrolled,
                        SUM(f.new_students)         AS new_students,
                        SUM(f.returnees)            AS returnees,
                        SUM(f.transferees)          AS transferees,
                        SUM(f.graduates)            AS graduates,
                        SUM(f.dropouts)             AS dropouts,
                        SUM(f.shifters_out)         AS shifters_out,
                        SUM(f.shifters_in)          AS shifters_in,
                        AVG(f.dropout_rate)         AS dropout_rate,
                        AVG(f.retention_rate)       AS retention_rate
                    FROM gold.fact_enrollment_metrics f
                    JOIN gold.dim_time       dt  ON dt.time_id      = f.time_id
                    JOIN gold.dim_program    dp  ON dp.program_id   = f.program_id
                    JOIN gold.dim_year_level dyl ON dyl.year_level_id = f.year_level_id
                    GROUP BY
                        dt.academic_year, dt.semester, dt.sort_key,
                        dp.program_code, dp.program_name, dp.college,
                        dyl.year_level, dyl.is_irregular
                    ORDER BY dt.sort_key, dp.program_code, dyl.year_level
                    """
                )
            ).fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame([dict(r._mapping) for r in rows])
        df = df.fillna(0)
        return df

    # ------------------------------------------------------------------
    # Feature engineering
    # ------------------------------------------------------------------

    def _engineer_features(
        self, df: pd.DataFrame
    ) -> tuple[np.ndarray, np.ndarray, list[str]]:
        """
        Engineer all features from the aggregated Gold data.

        Returns (X, y, feature_names).
        """
        fe = df.copy()

        # Rate features (safe division)
        def safe_rate(numerator, denominator):
            return np.where(denominator > 0, numerator / denominator * 100, 0.0)

        fe["dropout_rate"]       = safe_rate(fe["dropouts"],     fe["total_enrolled"])
        fe["new_student_rate"]   = safe_rate(fe["new_students"], fe["total_enrolled"])
        fe["returnee_rate"]      = safe_rate(fe["returnees"],    fe["total_enrolled"])
        fe["shifter_out_rate"]   = safe_rate(fe["shifters_out"], fe["total_enrolled"])
        fe["net_shifter_balance"]= fe["shifters_in"] - fe["shifters_out"]

        # Derived flags
        fe["is_super_senior"]    = (fe["year_level"] >= Thresholds.SUPER_SENIOR_YEAR_LEVEL).astype(int)
        fe["is_irregular"]       = fe["is_irregular"].astype(int)
        fe["log_enrollment"]     = np.log1p(fe["total_enrolled"])

        # Feature set
        feature_names = [
            "year_level",
            "is_super_senior",
            "is_irregular",
            "semester",
            "dropout_rate",
            "new_student_rate",
            "returnee_rate",
            "shifter_out_rate",
            "net_shifter_balance",
            "log_enrollment",
        ]

        X = fe[feature_names].values.astype(float)

        # Target: 1 if dropout_rate >= threshold
        y = (fe["dropout_rate"] >= self.DROPOUT_RISK_THRESHOLD).astype(int).values

        return X, y, feature_names

    # ------------------------------------------------------------------
    # Model training
    # ------------------------------------------------------------------

    def _train_random_forest(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_test:  np.ndarray,
        y_test:  np.ndarray,
        feature_names: list[str],
    ) -> ModelMetrics:
        """Train Random Forest and return evaluation metrics."""
        self._rf_model = RandomForestClassifier(
            n_estimators=200,
            max_depth=6,
            min_samples_leaf=2,
            class_weight="balanced",   # handles class imbalance
            random_state=42,
            n_jobs=-1,
        )
        self._rf_model.fit(X_train, y_train)

        metrics = self._evaluate(self._rf_model, X_train, y_train, X_test, y_test, "Random Forest")
        return metrics

    def _train_logistic_regression(
        self,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_test:  np.ndarray,
        y_test:  np.ndarray,
    ) -> ModelMetrics:
        """Train Logistic Regression baseline and return evaluation metrics."""
        lr = LogisticRegression(
            class_weight="balanced",
            max_iter=1000,
            random_state=42,
        )
        lr.fit(X_train, y_train)
        return self._evaluate(lr, X_train, y_train, X_test, y_test, "Logistic Regression")

    def _evaluate(
        self,
        model,
        X_train: np.ndarray,
        y_train: np.ndarray,
        X_test:  np.ndarray,
        y_test:  np.ndarray,
        label:   str,
    ) -> ModelMetrics:
        """Evaluate a trained classifier with cross-validation."""
        metrics = ModelMetrics()

        if len(X_test) == 0:
            logger.warning("No test samples for {} evaluation.", label)
            return metrics

        y_pred = model.predict(X_test)

        metrics.accuracy  = round(float(accuracy_score(y_test, y_pred)), 4)
        metrics.precision = round(float(precision_score(y_test, y_pred, zero_division=0)), 4)
        metrics.recall    = round(float(recall_score(y_test, y_pred, zero_division=0)), 4)
        metrics.f1_score  = round(float(f1_score(y_test, y_pred, zero_division=0)), 4)

        if len(np.unique(y_test)) > 1:
            y_prob = (
                model.predict_proba(X_test)[:, 1]
                if hasattr(model, "predict_proba")
                else y_pred.astype(float)
            )
            metrics.roc_auc = round(float(roc_auc_score(y_test, y_prob)), 4)

        # Stratified cross-validation on training data
        if len(np.unique(y_train)) > 1 and y_train.sum() >= 3:
            try:
                cv = StratifiedKFold(n_splits=min(5, int(y_train.sum())), shuffle=True, random_state=42)
                cv_scores = cross_val_score(model, X_train, y_train, cv=cv, scoring="f1")
                metrics.cv_f1_mean = round(float(cv_scores.mean()), 4)
                metrics.cv_f1_std  = round(float(cv_scores.std()), 4)
            except Exception as exc:
                logger.debug("CV skipped for {}: {}", label, exc)

        logger.info(
            "{} — Accuracy={} | F1={} | ROC-AUC={} | CV-F1={}±{}",
            label,
            metrics.accuracy, metrics.f1_score, metrics.roc_auc,
            metrics.cv_f1_mean, metrics.cv_f1_std,
        )
        return metrics

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def _predict_at_risk(
        self,
        df: pd.DataFrame,
        X_scaled: np.ndarray,
        feature_names: list[str],
    ) -> list[AtRiskRecord]:
        """Generate at-risk predictions and rules-based risk scores for each group."""
        records: list[AtRiskRecord] = []

        for i, (_, row) in enumerate(df.iterrows()):
            total_enrolled = int(row["total_enrolled"])
            dropouts       = int(row["dropouts"])
            dropout_rate   = float(row["dropout_rate"]) if row["dropout_rate"] else 0.0

            # Rules-based risk score (always computed)
            risk = RiskRules.compute_risk_score(
                dropout_rate=dropout_rate,
                year_level=int(row["year_level"]),
                total_enrolled=total_enrolled,
                shifters_out=int(row["shifters_out"]),
            )

            # ML prediction (if model is available and row is in test set)
            is_at_risk = risk.risk_level.value in ("High", "Critical")
            if self._rf_model is not None and i < len(X_scaled):
                try:
                    prob    = self._rf_model.predict_proba(X_scaled[i:i+1])[0][1]
                    is_at_risk = prob >= 0.5
                except Exception:
                    pass

            # Top risk factors (human-readable)
            top_factors = self._top_risk_factors(row, risk)

            records.append(
                AtRiskRecord(
                    program_code=str(row["program_code"]),
                    program_name=str(row["program_name"]),
                    college=str(row["college"]),
                    academic_year=str(row["academic_year"]),
                    semester=int(row["semester"]),
                    year_level=int(row["year_level"]),
                    total_enrolled=total_enrolled,
                    dropout_rate=round(dropout_rate, 2),
                    risk_score=risk.composite_score,
                    risk_label=risk.risk_level.value,
                    is_at_risk=is_at_risk,
                    top_risk_factors=top_factors,
                )
            )

        return sorted(records, key=lambda r: r.risk_score, reverse=True)

    @staticmethod
    def _top_risk_factors(row, risk) -> list[str]:
        """Return top 3 human-readable risk factors for a group."""
        factors = []
        if risk.dropout_component >= 0.5:
            factors.append(f"High dropout rate ({row['dropout_rate']:.1f}%)")
        if int(row["year_level"]) >= Thresholds.SUPER_SENIOR_YEAR_LEVEL:
            factors.append(f"Super senior students (Year {int(row['year_level'])})")
        if risk.shifter_component >= 0.3:
            factors.append(f"High shifter outflow ({int(row['shifters_out'])} students)")
        if not factors:
            factors.append("Low composite risk")
        return factors[:3]

    # ------------------------------------------------------------------
    # Feature importances
    # ------------------------------------------------------------------

    def _get_feature_importances(
        self, feature_names: list[str]
    ) -> list[FeatureImportance]:
        importances = self._rf_model.feature_importances_
        ranked = sorted(
            zip(feature_names, importances),
            key=lambda x: x[1],
            reverse=True,
        )
        return [FeatureImportance(f, round(float(i), 4)) for f, i in ranked]

    # ------------------------------------------------------------------
    # Rules-based fallback
    # ------------------------------------------------------------------

    def _rules_based_fallback(self) -> AtRiskReport:
        """
        Use rules_engine.RiskRules alone when there is insufficient data
        to train a classifier. No ML model is saved.
        """
        logger.info("Running rules-based risk scoring (no ML model).")
        report = AtRiskReport(generated_at=pd.Timestamp.now().isoformat())

        try:
            df = self._load_feature_data()
            if df.empty:
                report.status = "success"
                return report

            latest_sort = df["sort_key"].max()
            latest_df   = df[df["sort_key"] == latest_sort]

            records = []
            for _, row in latest_df.iterrows():
                dropout_rate = float(row.get("dropout_rate", 0))
                risk = RiskRules.compute_risk_score(
                    dropout_rate=dropout_rate,
                    year_level=int(row["year_level"]),
                    total_enrolled=int(row["total_enrolled"]),
                    shifters_out=int(row["shifters_out"]),
                )
                records.append(
                    AtRiskRecord(
                        program_code=str(row["program_code"]),
                        program_name=str(row["program_name"]),
                        college=str(row["college"]),
                        academic_year=str(row["academic_year"]),
                        semester=int(row["semester"]),
                        year_level=int(row["year_level"]),
                        total_enrolled=int(row["total_enrolled"]),
                        dropout_rate=round(dropout_rate, 2),
                        risk_score=risk.composite_score,
                        risk_label=risk.risk_level.value,
                        is_at_risk=risk.risk_level.value in ("High", "Critical"),
                        top_risk_factors=self._top_risk_factors(row, risk),
                    )
                )

            report.at_risk_records = sorted(records, key=lambda r: r.risk_score, reverse=True)
            report.high_risk_count = sum(1 for r in records if r.risk_label in ("High", "Critical"))
            report.critical_risk_count = sum(1 for r in records if r.risk_label == "Critical")
            report.total_at_risk_enrolled = sum(r.total_enrolled for r in records if r.is_at_risk)
            report.status = "success"

        except Exception as exc:
            report.status = "failed"
            report.error_message = str(exc)

        return report

    # ------------------------------------------------------------------
    # Model persistence
    # ------------------------------------------------------------------

    def _save_model(self) -> None:
        """Save the trained Random Forest model and scaler to disk."""
        if self._rf_model is None:
            return
        try:
            model_path  = _config.models_path / "dropout_classifier.pkl"
            scaler_path = _config.models_path / "dropout_scaler.pkl"
            model_path.parent.mkdir(parents=True, exist_ok=True)

            with open(model_path,  "wb") as f:
                pickle.dump(self._rf_model, f)
            with open(scaler_path, "wb") as f:
                pickle.dump(self._scaler, f)

            logger.info("At-risk model saved → {}", model_path.name)
        except Exception as exc:
            logger.warning("Could not save at-risk model: {}", exc)

    # ------------------------------------------------------------------
    # Console summary
    # ------------------------------------------------------------------

    def print_summary(self, report: AtRiskReport) -> None:
        sep = "=" * 65
        print(sep)
        print("  NEUST Analytics — At-Risk Model Summary")
        print(sep)
        print(f"  Random Forest  F1={report.rf_metrics.f1_score} | "
              f"ROC-AUC={report.rf_metrics.roc_auc} | "
              f"CV-F1={report.rf_metrics.cv_f1_mean}±{report.rf_metrics.cv_f1_std}")
        print(f"  Logistic Reg.  F1={report.lr_metrics.f1_score} | "
              f"ROC-AUC={report.lr_metrics.roc_auc}")
        print(sep)
        print(f"  High/Critical Risk Groups : {report.high_risk_count}")
        print(f"  Critical Risk Groups      : {report.critical_risk_count}")
        print(f"  At-Risk Enrolled Students : {report.total_at_risk_enrolled:,}")
        print(sep)

        if report.feature_importances:
            print("  Top Feature Importances:")
            for fi in report.feature_importances[:5]:
                bar = "█" * int(fi.importance * 30)
                print(f"    {fi.feature:25s} {fi.importance:.4f}  {bar}")
            print(sep)

        if report.at_risk_records:
            high_risk = [r for r in report.at_risk_records if r.is_at_risk][:5]
            if high_risk:
                print("  Top At-Risk Groups:")
                for r in high_risk:
                    print(
                        f"    {r.program_code:10s} Yr{r.year_level} | "
                        f"Score={r.risk_score:.2f} [{r.risk_label}] | "
                        f"Dropout={r.dropout_rate}% | "
                        f"Enrolled={r.total_enrolled}"
                    )
        print(sep)


# ==============================================================================
# Module-level runner — called by pipeline.py
# ==============================================================================

def run_at_risk_model() -> AtRiskReport:
    """
    Entry point called by pipeline.py.

    Usage:
        from analytics.at_risk_model import run_at_risk_model
        report = run_at_risk_model()
    """
    model = AtRiskModel()
    report = model.run()
    model.print_summary(report)
    return report