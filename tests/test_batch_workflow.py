"""Batch observation windows and report delivery preserve workflow semantics."""
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from workflow_agents.common.model_files import _replace_swmm_event_inputs
import test_investigation as fixture


class FixedObservationTests(unittest.TestCase):
    def write_case(self, fixed_end):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        source = root / "source.inp"
        source.write_text("[OPTIONS]\nFLOW_UNITS LPS\n[RAINGAGES]\nRG1 INTENSITY 00:05 1 TIMESERIES R\n[TIMESERIES]\nR 01/01/2024 00:00 0\n", encoding="utf-8")
        timing = {"post_rainfall_hours": 3, "swmm_save_interval_minutes": 5 / 60,
                  "simulation_end_time": fixed_end}
        return _replace_swmm_event_inputs(source, root / "output.inp",
                   [(datetime(2024, 1, 1, 0, 0), 12), (datetime(2024, 1, 1, 0, 5), 0),
                    (datetime(2024, 1, 1, 4, 0), 0)], timing=timing)

    def test_trailing_zero_does_not_shorten_explicit_observation(self):
        result = self.write_case("2024-01-01 07:00:00")
        self.assertEqual(result["rainfall_end"], "2024-01-01 00:05:00")
        self.assertEqual(result["simulation_end"], "2024-01-01 07:00:00")

    def test_fixed_end_cannot_truncate_required_recovery(self):
        with self.assertRaises(ValueError):
            self.write_case("2024-01-01 02:00:00")


class StructuredBatchReportTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.InvestigationTests.asyncSetUp
    asyncTearDown = fixture.InvestigationTests.asyncTearDown
    prepare = fixture.InvestigationTests.prepare
    step = fixture.InvestigationTests.step
    async def test_deferred_html_keeps_verified_markdown_without_model_call(self):
        await self.prepare()
        await self.step("diagnose", AsyncMock(return_value=fixture.answer()))
        await self.step("verify")
        with patch("workflow_agents.reporting.pipeline.attach_display_report", new_callable=AsyncMock) as html:
            state = await advance_report(self.task)
        html.assert_not_called()
        self.assertEqual(state["report_revision"], state["revision"])
        self.assertTrue(Path(state["report_file"]).is_file())


async def advance_report(task_id):
    from workflow_agents.orchestration.investigation import advance_investigation
    return await advance_investigation("m", "r", "report", task_id, include_display_report=False)
