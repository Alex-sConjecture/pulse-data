# Recidiviz - a data platform for criminal justice reform
# Copyright (C) 2019 Recidiviz, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
# =============================================================================
"""Manages the views needed for dashboard calculations.

This is run locally whenever the views need to be updated.

If running locally, be sure to have GOOGLE_CLOUD_PROJECT set as an environment
variable.
"""

from typing import List

from recidiviz.calculator.query import bqview, bq_utils

from recidiviz.calculator.query.state import view_config
from recidiviz.calculator.query.state.views.admissions import admissions_views
from recidiviz.calculator.query.state.views.program_evaluation import \
    program_evaluation_views
from recidiviz.calculator.query.state.views.reference import reference_views
from recidiviz.calculator.query.state.views.reincarcerations import \
    reincarcerations_views
from recidiviz.calculator.query.state.views.revocation_analysis import \
    revocation_analysis_views
from recidiviz.calculator.query.state.views.revocations import revocations_views
from recidiviz.calculator.query.state.views.supervision import supervision_views

VIEWS_TO_UPDATE: List[bqview.BigQueryView] = \
    reference_views.REF_VIEWS + \
    admissions_views.ADMISSIONS_VIEWS + \
    reincarcerations_views.REINCARCERATIONS_VIEWS + \
    revocations_views.REVOCATIONS_VIEWS + \
    supervision_views.SUPERVISION_VIEWS + \
    program_evaluation_views.PROGRAM_EVALUATION_VIEWS + \
    revocation_analysis_views.REVOCATION_ANALYSIS_VIEWS

# Views that rely on new data from a Dataflow job
DATAFLOW_VIEWS: List[bqview.BigQueryView] = \
    reincarcerations_views.REINCARCERATIONS_VIEWS + \
    [reference_views.MOST_RECENT_JOB_ID_BY_METRIC_AND_STATE_CODE_VIEW]


def create_dataset_and_update_views(
        dataset_name: str,
        views_to_update: List[bqview.BigQueryView]):
    """Create and update Views and their parent Dataset.

    Create a parent Views dataset if it does not exist, and
    creates or updates the underlying Views as defined in
    recidiviz.calculator.bq.views.bqview

    Args:
        dataset_name: Name of BigQuery dataset to contain Views. Gets created
            if it does not already exist.
        views_to_update: View objects to be created or updated.
    """
    views_dataset_ref = bq_utils.client().dataset(dataset_name)
    bq_utils.create_dataset_if_necessary(views_dataset_ref)

    for view in views_to_update:
        bq_utils.create_or_update_view(views_dataset_ref, view)


if __name__ == '__main__':
    create_dataset_and_update_views(view_config.DASHBOARD_VIEWS_DATASET,
                                    VIEWS_TO_UPDATE)
