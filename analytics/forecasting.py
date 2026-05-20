# ==============================================================================
# NEUST Academic Analytics and Forecasting System
# analytics/forecasting.py
# Prophet-based enrollment forecasting with ARIMA validation
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

warnings.filterwarnings("ignore", category=FutureWarning)

from database.connection import get_session
from utils.config import get_config
from utils.logger import log_step_failure, log_step_start, log_step_success, logger

_config = get_config()

# Prophet import — graceful failure if not installed
try:
    from prophet import Prophet
    PROPHET_AVAILABLE = True
except ImportError:
    PROPHET_AVAILABLE = False
    logger.warning("Prophet not installed — run: pip install prophet")

# Statsmodels for ARIMA validation
try:
    from statsmodels.tsa.arima.model import ARIMA
    ARIMA_AVAILABLE = True
except ImportError:
    ARIMA_AVAILABLE = False
    logger.warning("statsmodels not installed — ARIMA validation disabled")


# ==============================================================================
# Result containers
# ==============================================================================

@dataclass
class ForecastMetrics:
    """Evaluation metrics for a single forecast model."""
    mae:  float | None = None   # Mean Absolute Error
    rmse: float | None = None   # Root Mean Squared Error
    mape: float | None = None   # Mean Absolute Percentage Error (%)


@dataclass
class ProgramForecast:
    """Forecast output for a single program."""
    program_code:       str
    program_name:       str
    college:            str
    historical_df:      pd.DataFrame = field(default_factory=pd.DataFrame)
    forecast_df:        pd.DataFrame = field(default_factory=pd.DataFrame)
    prophet_metrics:    ForecastMetrics = field(default_factory=ForecastMetrics)
    arima_metrics:      ForecastMetrics = field(default_factory=ForecastMetrics)
    model_path:         str | None = None
    semesters_forecast: int = 0


@dataclass
class ForecastReport:
    """Full forecast output across all programs."""
    generated_at:           str = ""
    programs:               list[ProgramForecast] = field(default_factory=list)
    institution_forecast:   pd.DataFrame = field(default_factory=pd.DataFrame)
    semesters_forecast:     int = 0
    elapsed_seconds:        float = 0.0
    status:                 str = "pending"
    error_message:          str | None = None


# ==============================================================================
# Forecasting engine
# ==============================================================================

class EnrollmentForecaster:
    """
    Forecasts enrollment using Prophet as the primary model,
    with ARIMA as a validation/comparison model.

    Data preparation:
        - Pulls total_enrolled per (academic_year, semester, program_code) from Gold
        - Converts sort_key to a datetime ds column (Prophet requires datetime)
        - Semester 1 → Feb 1,  Semester 2 → Aug 1,  Summer → Dec 1

    Forecasting strategy:
        - Train on all available historical data
        - Forecast next N semesters (default: 4 = 2 academic years)
        - Evaluate using last 2 semesters as holdout (backtesting)
        - Save trained Prophet model as .pkl for reuse

    Usage:
        forecaster = EnrollmentForecaster(semesters_ahead=4)
        report = forecaster.run()
    """

    # Semester → approximate calendar month mapping
    SEMESTER_MONTH = {1: 2, 2: 8, 3: 12}

    def __init__(self, semesters_ahead: int = 4) -> None:
        self.semesters_ahead = semesters_ahead
        self._started        = time.monotonic()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> ForecastReport:
        """Run enrollment forecasting for all programs + institution-wide."""
        report = ForecastReport(
            generated_at=pd.Timestamp.now().isoformat(),
            semesters_forecast=self.semesters_ahead,
        )
        log_step_start(5, "Enrollment forecasting")

        if not PROPHET_AVAILABLE:
            report.status        = "failed"
            report.error_message = "Prophet not installed — run: pip install prophet"
            logger.error(report.error_message)
            return report

        try:
            # Load Gold enrollment data
            df = self._load_enrollment_series()
            if df.empty:
                logger.warning("No enrollment data found — skipping forecasting.")
                report.status = "success"
                return report

            logger.info(
                "Loaded enrollment series — {} rows | {} programs",
                len(df), df["program_code"].nunique(),
            )

            # Institution-wide forecast
            logger.info("Running institution-wide forecast ...")
            report.institution_forecast = self._forecast_institution(df)

            # Per-program forecasts
            programs = df["program_code"].unique()
            logger.info("Running per-program forecasts for {} programs ...", len(programs))

            for program_code in programs:
                program_df = df[df["program_code"] == program_code].copy()
                if len(program_df) < 4:
                    logger.warning(
                        "Program {} has only {} data points — skipping (need ≥4).",
                        program_code, len(program_df),
                    )
                    continue

                prog_meta = program_df.iloc[0]
                try:
                    prog_forecast = self._forecast_program(
                        program_df=program_df,
                        program_code=program_code,
                        program_name=prog_meta.get("program_name", program_code),
                        college=prog_meta.get("college", "Unknown"),
                    )
                    report.programs.append(prog_forecast)
                    logger.debug(
                        "Forecast complete — {} | MAPE={:.1f}%",
                        program_code,
                        prog_forecast.prophet_metrics.mape or 0,
                    )
                except Exception as exc:
                    logger.warning("Forecast failed for {}: {}", program_code, exc)

            report.status = "success"

        except Exception as exc:
            report.status        = "failed"
            report.error_message = str(exc)
            logger.exception("Forecasting failed: {}", exc)
            log_step_failure(5, "Enrollment forecasting", exc)

        finally:
            report.elapsed_seconds = time.monotonic() - self._started

        if report.status == "success":
            log_step_success(5, "Enrollment forecasting")
            logger.info(
                "Forecasting complete — {} programs | {:.2f}s",
                len(report.programs), report.elapsed_seconds,
            )

        return report

    # ------------------------------------------------------------------
    # Data loading
    # ------------------------------------------------------------------

    def _load_enrollment_series(self) -> pd.DataFrame:
        """Load historical enrollment per program per semester from Gold."""
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
                        SUM(f.total_enrolled) AS total_enrolled
                    FROM gold.fact_enrollment_metrics f
                    JOIN gold.dim_time    dt ON dt.time_id    = f.time_id
                    JOIN gold.dim_program dp ON dp.program_id = f.program_id
                    GROUP BY
                        dt.academic_year, dt.semester, dt.sort_key,
                        dp.program_code, dp.program_name, dp.college
                    ORDER BY dp.program_code, dt.sort_key
                    """
                )
            ).fetchall()

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame([dict(r._mapping) for r in rows])
        df["ds"] = df.apply(self._sort_key_to_date, axis=1)
        df["y"]  = df["total_enrolled"].astype(float)
        return df

    def _sort_key_to_date(self, row) -> pd.Timestamp:
        """Convert sort_key to a representative calendar date for Prophet."""
        year_start = int(row["sort_key"]) // 10
        semester   = int(row["sort_key"]) % 10
        month      = self.SEMESTER_MONTH.get(semester, 2)
        return pd.Timestamp(year=year_start, month=month, day=1)

    # ------------------------------------------------------------------
    # Institution-wide forecast
    # ------------------------------------------------------------------

    def _forecast_institution(self, df: pd.DataFrame) -> pd.DataFrame:
        """Aggregate all programs and forecast institution-wide enrollment."""
        inst_df = (
            df.groupby("ds")["y"]
            .sum()
            .reset_index()
            .sort_values("ds")
        )

        if len(inst_df) < 4:
            logger.warning("Insufficient institution data for forecasting.")
            return pd.DataFrame()

        forecast_df, _ = self._run_prophet(
            series_df=inst_df,
            label="Institution-wide",
            save_path=_config.models_path / "prophet_institution.pkl",
        )
        return forecast_df

    # ------------------------------------------------------------------
    # Per-program forecast
    # ------------------------------------------------------------------

    def _forecast_program(
        self,
        program_df: pd.DataFrame,
        program_code: str,
        program_name: str,
        college: str,
    ) -> ProgramForecast:
        """Forecast enrollment for a single program."""
        series_df = (
            program_df[["ds", "y"]]
            .sort_values("ds")
            .reset_index(drop=True)
        )

        model_path = _config.models_path / f"prophet_{program_code.lower()}.pkl"

        forecast_df, prophet_metrics = self._run_prophet(
            series_df=series_df,
            label=program_code,
            save_path=model_path,
        )

        arima_metrics = ForecastMetrics()
        if ARIMA_AVAILABLE and len(series_df) >= 6:
            arima_metrics = self._run_arima_validation(series_df, program_code)

        return ProgramForecast(
            program_code=program_code,
            program_name=program_name,
            college=college,
            historical_df=series_df,
            forecast_df=forecast_df,
            prophet_metrics=prophet_metrics,
            arima_metrics=arima_metrics,
            model_path=str(model_path),
            semesters_forecast=self.semesters_ahead,
        )

    # ------------------------------------------------------------------
    # Prophet runner
    # ------------------------------------------------------------------

    def _run_prophet(
        self,
        series_df: pd.DataFrame,
        label: str,
        save_path: Path,
    ) -> tuple[pd.DataFrame, ForecastMetrics]:
        """
        Train Prophet model, generate forecast, and evaluate.

        Prophet configuration:
            - yearly_seasonality=True  — captures academic year cycles
            - weekly_seasonality=False — irrelevant for semester data
            - daily_seasonality=False  — irrelevant
            - changepoint_prior_scale=0.1 — moderate flexibility
        """
        # Holdout: last 2 semesters for evaluation
        holdout_n = min(2, len(series_df) - 2)
        train_df  = series_df.iloc[:-holdout_n] if holdout_n > 0 else series_df
        test_df   = series_df.iloc[-holdout_n:]  if holdout_n > 0 else pd.DataFrame()

        # Train
        model = Prophet(
            yearly_seasonality=True,
            weekly_seasonality=False,
            daily_seasonality=False,
            changepoint_prior_scale=0.1,
            seasonality_mode="additive",
        )
        with _suppress_prophet_output():
            model.fit(train_df)

        # Forecast horizon: historical + future semesters
        future_periods = holdout_n + self.semesters_ahead
        future = model.make_future_dataframe(periods=future_periods * 6, freq="MS")
        forecast = model.predict(future)

        # Align forecast dates with actual semester dates
        forecast_trimmed = forecast[["ds", "yhat", "yhat_lower", "yhat_upper"]].copy()
        forecast_trimmed["yhat"] = forecast_trimmed["yhat"].clip(lower=0).round(0)
        forecast_trimmed["yhat_lower"] = forecast_trimmed["yhat_lower"].clip(lower=0).round(0)
        forecast_trimmed["yhat_upper"] = forecast_trimmed["yhat_upper"].clip(lower=0).round(0)

        # Evaluate on holdout
        metrics = ForecastMetrics()
        if not test_df.empty:
            test_preds = forecast_trimmed[
                forecast_trimmed["ds"].isin(test_df["ds"])
            ]["yhat"].values
            actual     = test_df["y"].values

            if len(test_preds) == len(actual) and len(actual) > 0:
                errors = actual - test_preds
                metrics.mae  = round(float(np.mean(np.abs(errors))), 2)
                metrics.rmse = round(float(np.sqrt(np.mean(errors ** 2))), 2)
                nonzero_mask = actual != 0
                if nonzero_mask.any():
                    metrics.mape = round(
                        float(np.mean(np.abs(errors[nonzero_mask] / actual[nonzero_mask])) * 100),
                        2,
                    )

        # Save model
        try:
            save_path.parent.mkdir(parents=True, exist_ok=True)
            with open(save_path, "wb") as f:
                pickle.dump(model, f)
            logger.debug("Prophet model saved → {}", save_path.name)
        except Exception as exc:
            logger.warning("Could not save Prophet model for {}: {}", label, exc)

        return forecast_trimmed, metrics

    # ------------------------------------------------------------------
    # ARIMA validation runner
    # ------------------------------------------------------------------

    def _run_arima_validation(
        self, series_df: pd.DataFrame, label: str
    ) -> ForecastMetrics:
        """
        Train ARIMA(1,1,1) as a validation model and compute error metrics.
        Used to benchmark Prophet accuracy — not used for final forecasts.
        """
        metrics = ForecastMetrics()
        try:
            y = series_df["y"].values
            holdout_n = min(2, len(y) - 4)
            if holdout_n < 1:
                return metrics

            train_y = y[:-holdout_n]
            test_y  = y[-holdout_n:]

            model = ARIMA(train_y, order=(1, 1, 1))
            fit   = model.fit()
            preds = fit.forecast(steps=holdout_n)

            errors = test_y - preds
            metrics.mae  = round(float(np.mean(np.abs(errors))), 2)
            metrics.rmse = round(float(np.sqrt(np.mean(errors ** 2))), 2)
            nonzero_mask = test_y != 0
            if nonzero_mask.any():
                metrics.mape = round(
                    float(np.mean(np.abs(errors[nonzero_mask] / test_y[nonzero_mask])) * 100),
                    2,
                )
        except Exception as exc:
            logger.debug("ARIMA validation skipped for {}: {}", label, exc)

        return metrics

    # ------------------------------------------------------------------
    # Export helpers
    # ------------------------------------------------------------------

    def export_forecast_csv(
        self, report: ForecastReport, output_path: Path | None = None
    ) -> Path:
        """
        Export all program forecasts to a single CSV file.
        Saved to data/exports/ by default.
        """
        if not report.programs:
            logger.warning("No program forecasts to export.")
            return Path()

        output_path = output_path or (
            _config.exports_path / f"forecast_output_{pd.Timestamp.now().strftime('%Y%m%d')}.csv"
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)

        rows = []
        for prog in report.programs:
            if prog.forecast_df.empty:
                continue
            future_rows = prog.forecast_df[
                prog.forecast_df["ds"] > prog.historical_df["ds"].max()
            ].copy()
            future_rows["program_code"] = prog.program_code
            future_rows["program_name"] = prog.program_name
            future_rows["college"]      = prog.college
            rows.append(future_rows)

        if rows:
            export_df = pd.concat(rows, ignore_index=True)
            export_df.to_csv(output_path, index=False)
            logger.info("Forecast CSV exported → {}", output_path)

        return output_path


# ==============================================================================
# Context manager to suppress Prophet's verbose stdout
# ==============================================================================

import contextlib
import os
import sys

@contextlib.contextmanager
def _suppress_prophet_output():
    """Suppress Prophet's cmdstanpy output during fitting."""
    with open(os.devnull, "w") as devnull:
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        try:
            sys.stdout = devnull
            sys.stderr = devnull
            yield
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr


# ==============================================================================
# Module-level runner — called by pipeline.py
# ==============================================================================

def run_forecast(semesters_ahead: int = 4) -> ForecastReport:
    """
    Entry point called by pipeline.py.

    Usage:
        from analytics.forecasting import run_forecast
        report = run_forecast(semesters_ahead=4)
    """
    forecaster = EnrollmentForecaster(semesters_ahead=semesters_ahead)
    report = forecaster.run()

    if report.status == "success" and report.programs:
        forecaster.export_forecast_csv(report)

    return report