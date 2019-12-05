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
"""Runs the supervision calculation pipeline.

usage: pipeline.py --output=OUTPUT_LOCATION --project=PROJECT
                    --dataset=DATASET --methodology=METHODOLOGY
                    [--include_age] [--include_gender]
                    [--include_race] [--include_ethnicity]

Example output to GCP storage bucket:
python -m recidiviz.calculator.supervision.pipeline
        --project=recidiviz-project-name
        --dataset=recidiviz-project-name.dataset
        --output=gs://recidiviz-bucket/output_location
            --methodology=BOTH

Example output to local file:
python -m recidiviz.calculator.supervision.pipeline
        --project=recidiviz-project-name
        --dataset=recidiviz-project-name.dataset
        --output=output_file --methodology=PERSON

Example output including race and gender dimensions:
python -m recidiviz.calculator.supervision.pipeline
        --project=recidiviz-project-name
        --dataset=recidiviz-project-name.dataset
        --output=output_file --methodology=EVENT
            --include_race=True --include_gender=True

"""
import argparse
import datetime
import json
import logging
from typing import Dict, Any, List, Tuple

import apache_beam as beam
from apache_beam.options.pipeline_options import PipelineOptions
from apache_beam.options.pipeline_options import SetupOptions
from apache_beam.typehints import with_input_types, with_output_types
from more_itertools import one

from recidiviz.calculator.pipeline.supervision import identifier, calculator
from recidiviz.calculator.pipeline.supervision.metrics import \
    SupervisionPopulationMetric, SupervisionMetric
from recidiviz.calculator.pipeline.supervision.metrics import \
    SupervisionMetricType as MetricType
from recidiviz.calculator.pipeline.supervision.supervision_month import \
    SupervisionMonth
from recidiviz.calculator.pipeline.utils.beam_utils import SumFn
from recidiviz.calculator.pipeline.utils.entity_hydration_utils import \
    SetViolationResponseOnIncarcerationPeriod
from recidiviz.calculator.pipeline.utils.execution_utils import get_job_id
from recidiviz.calculator.pipeline.utils.extractor_utils import BuildRootEntity
from recidiviz.calculator.pipeline.utils.metric_utils import \
    json_serializable_metric_key, MetricMethodologyType
from recidiviz.persistence.database.schema.state import schema
from recidiviz.persistence.entity.state import entities
from recidiviz.utils import environment

# Cached job_id value
_job_id = None


def job_id(pipeline_options: Dict[str, str]) -> str:
    global _job_id
    if not _job_id:
        _job_id = get_job_id(pipeline_options)
    return _job_id


@environment.test_only
def clear_job_id():
    global _job_id
    _job_id = None


@with_input_types(beam.typehints.Tuple[int, Dict[str, Any]])
@with_output_types(beam.typehints.Tuple[entities.StatePerson,
                                        List[SupervisionMonth]])
class GetSupervisionMonths(beam.PTransform):
    """Transforms a StatePerson and their periods of supervision and
     incarceration into SupervisionMonths."""

    def __init__(self):
        super(GetSupervisionMonths, self).__init__()

    def expand(self, input_or_inputs):
        return (input_or_inputs
                | beam.ParDo(ClassifySupervisionMonths()))


@with_input_types(beam.typehints.Tuple[entities.StatePerson,
                                       List[SupervisionMonth]])
@with_output_types(SupervisionMetric)
class GetSupervisionMetrics(beam.PTransform):
    """Transforms a StatePerson and their SupervisionMonths into
    SupervisionMetrics."""

    def __init__(self, pipeline_options: Dict[str, str],
                 inclusions: Dict[str, bool]):
        super(GetSupervisionMetrics, self).__init__()
        self._pipeline_options = pipeline_options
        self.inclusions = inclusions

    def expand(self, input_or_inputs):
        # Calculate supervision metric combinations from a StatePerson and their
        # SupervisionMonths
        supervision_metric_combinations = (
            input_or_inputs
            | 'Map to metric combinations' >>
            beam.ParDo(CalculateSupervisionMetricCombinations(),
                       **self.inclusions).with_outputs('populations'))

        # Calculate the supervision population values for the metrics combined
        # by key
        populations_with_sums = (supervision_metric_combinations.populations
                                 | 'Calculate supervision population values' >>
                                 beam.CombinePerKey(SumFn()))

        # Produce the SupervisionPopulationMetrics
        population_metrics = (populations_with_sums
                              | 'Produce supervision population metrics' >>
                              beam.ParDo(
                                  ProduceSupervisionPopulationMetric(),
                                  **self._pipeline_options))

        # Merge the metric groups
        merged_metrics = ([population_metrics]
                          | 'Merge population metrics' >>
                          beam.Flatten())

        # Return SupervisionMetrics objects
        return merged_metrics


@with_input_types(beam.typehints.Tuple[int, Dict[str, Any]])
@with_output_types(beam.typehints.Tuple[entities.StatePerson,
                                        List[SupervisionMonth]])
class ClassifySupervisionMonths(beam.DoFn):
    """Classifies time on supervision as months with or without revocation."""

    def process(self, element, *args, **kwargs):
        """Identifies instances of revocation and non-revocation months on
        supervision.
        """
        _, person_periods = element

        # Get the StateSupervisionPeriods as a list
        supervision_periods = \
            list(person_periods['supervision_periods'])

        # Get the StateIncarcerationPeriods as a list
        incarceration_periods = \
            list(person_periods['incarceration_periods'])

        # Get the StatePerson
        person = one(person_periods['person'])

        # Find the SupervisionMonths from the supervision and incarceration
        # periods
        supervision_months = \
            identifier.find_supervision_months(
                supervision_periods,
                incarceration_periods)

        if not supervision_months:
            logging.info("No valid supervision months for person with"
                         "id: %d. Excluding them from the "
                         "calculations.", person.person_id)
        else:
            yield (person, supervision_months)

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(beam.typehints.Tuple[entities.StatePerson,
                                       List[SupervisionMonth]])
@with_output_types(beam.typehints.Tuple[str, Any])
class CalculateSupervisionMetricCombinations(beam.DoFn):
    """Calculates supervision metric combinations."""

    def process(self, element, *args, **kwargs):
        """Produces various supervision metric combinations.

        Sends the calculator the StatePerson entity and their corresponding
        SupervisionMonths for mapping all supervision combinations.

        Args:
            element: Tuple containing a StatePerson and their SupervisionMonths
            **kwargs: This should be a dictionary with values for the
                following keys:
                    - age_bucket
                    - gender
                    - race
                    - ethnicity
        Yields:
            Each supervision metric combination, tagged by metric type.
        """
        person, supervision_months = element

        # Calculate recidivism metric combinations for this person and events
        metric_combinations = \
            calculator.map_supervision_combinations(person,
                                                    supervision_months, kwargs)

        # Return each of the supervision metric combinations
        for metric_combination in metric_combinations:
            metric_key, value = metric_combination

            # Converting the metric key to a JSON string so it is hashable
            serializable_dict = json_serializable_metric_key(metric_key)
            json_key = json.dumps(serializable_dict, sort_keys=True)

            if metric_key.get('metric_type') == MetricType.POPULATION.value:
                yield beam.pvalue.TaggedOutput('populations',
                                               (json_key, value))

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(beam.typehints.Tuple[str, int],
                  **{'runner': str,
                     'project': str,
                     'job_name': str,
                     'region': str,
                     'job_timestamp': str}
                  )
@with_output_types(SupervisionPopulationMetric)
class ProduceSupervisionPopulationMetric(beam.DoFn):
    """Produces SupervisionPopulationMetrics."""

    def process(self, element, *args, **kwargs):
        pipeline_options = kwargs

        pipeline_job_id = job_id(pipeline_options)

        (metric_key, value) = element

        if value is None:
            # Due to how the pipeline arrives at this function, this should be
            # impossible.
            raise ValueError("No value associated with this metric key.")

        # Convert JSON string to dictionary
        dict_metric_key = json.loads(metric_key)

        if dict_metric_key.get('metric_type') == MetricType.POPULATION.value:
            # For count metrics, the value is the number of returns
            dict_metric_key['count'] = value

            supervision_metric = \
                SupervisionPopulationMetric. \
                build_from_metric_key_group(
                    dict_metric_key, pipeline_job_id)
        else:
            logging.error("Unexpected metric of type: %s",
                          dict_metric_key.get('metric_type'))
            return

        if supervision_metric:
            yield supervision_metric

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


@with_input_types(SupervisionMetric)
@with_output_types(beam.typehints.Dict[str, Any])
class SupervisionMetricWritableDict(beam.DoFn):
    """Builds a dictionary in the format necessary to write the output to
    BigQuery."""

    def process(self, element, *args, **kwargs):
        """The beam.io.WriteToBigQuery transform requires elements to be in
        dictionary form, where the values are in formats as required by BigQuery
        I/O connector.

        For a list of required formats, see the "Data types" section of:
            https://beam.apache.org/documentation/io/built-in/google-bigquery/

        Args:
            element: A SupervisionMetric

        Yields:
            A dictionary representation of the SupervisionMetric
                in the format Dict[str, Any] so that it can be written to
                BigQuery using beam.io.WriteToBigQuery.
        """
        element_dict = json_serializable_metric_key(element.__dict__)

        if isinstance(element, SupervisionPopulationMetric):
            yield beam.pvalue.TaggedOutput('populations', element_dict)

    def to_runner_api_parameter(self, _):
        pass  # Passing unused abstract method.


def parse_arguments(argv):
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser()

    # Parse arguments
    parser.add_argument('--input',
                        dest='input',
                        type=str,
                        help='BigQuery dataset to query.',
                        required=True)

    # TODO: Generalize these arguments
    parser.add_argument('--include_age',
                        dest='include_age',
                        type=bool,
                        help='Include metrics broken down by age.',
                        default=False)

    parser.add_argument('--include_gender',
                        dest='include_gender',
                        type=bool,
                        help='Include metrics broken down by gender.',
                        default=False)

    parser.add_argument('--include_race',
                        dest='include_race',
                        type=bool,
                        help='Include metrics broken down by race.',
                        default=False)

    parser.add_argument('--include_ethnicity',
                        dest='include_ethnicity',
                        type=bool,
                        help='Include metrics broken down by ethnicity.',
                        default=False)

    parser.add_argument('--methodology',
                        dest='methodology',
                        type=str,
                        choices=['PERSON', 'EVENT', 'BOTH'],
                        help='PERSON, EVENT, or BOTH',
                        required=True)

    parser.add_argument('--output',
                        dest='output',
                        type=str,
                        help='Output dataset to write results to.',
                        required=True)

    return parser.parse_known_args(argv)


def dimensions_and_methodologies(known_args) -> \
        Tuple[Dict[str, bool], List[MetricMethodologyType]]:

    filterable_dimensions_map = {
        'include_age': 'age_bucket',
        'include_ethnicity': 'ethnicity',
        'include_gender': 'gender',
        'include_race': 'race',
    }

    known_args_dict = vars(known_args)

    dimensions: Dict[str, bool] = {}

    for dimension_key in filterable_dimensions_map:
        if not known_args_dict[dimension_key]:
            dimensions[filterable_dimensions_map[dimension_key]] = False
        else:
            dimensions[filterable_dimensions_map[dimension_key]] = True

    methodologies = []

    if known_args.methodology == 'BOTH':
        methodologies.append(MetricMethodologyType.EVENT)
        methodologies.append(MetricMethodologyType.PERSON)
    else:
        methodologies.append(MetricMethodologyType[known_args.methodology])

    return dimensions, methodologies


def run(argv=None):
    """Runs the supervision calculation pipeline."""

    # Workaround to load SQLAlchemy objects at start of pipeline. This is
    # necessary because the BuildRootEntity function tries to access attributes
    # of relationship properties on the SQLAlchemy room_schema_class before they
    # have been loaded. However, if *any* SQLAlchemy objects have been
    # instantiated, then the relationship properties are loaded and their
    # attributes can be successfully accessed.
    _ = schema.StatePerson()

    # Parse command-line arguments
    known_args, pipeline_args = parse_arguments(argv)

    pipeline_options = PipelineOptions(pipeline_args)
    pipeline_options.view_as(SetupOptions).save_main_session = True

    # Get pipeline job details
    all_pipeline_options = pipeline_options.get_all_options()

    query_dataset = all_pipeline_options['project'] + '.' + known_args.input

    with beam.Pipeline(argv=pipeline_args) as p:
        # Get StatePersons
        persons = (p
                   | 'Load Persons' >>
                   BuildRootEntity(dataset=query_dataset,
                                   data_dict=None,
                                   root_schema_class=schema.StatePerson,
                                   root_entity_class=entities.StatePerson,
                                   unifying_id_field='person_id',
                                   build_related_entities=True))

        # Get StateIncarcerationPeriods
        incarceration_periods = (p
                                 | 'Load IncarcerationPeriods' >>
                                 BuildRootEntity(
                                     dataset=query_dataset,
                                     data_dict=None,
                                     root_schema_class=
                                     schema.StateIncarcerationPeriod,
                                     root_entity_class=
                                     entities.StateIncarcerationPeriod,
                                     unifying_id_field='person_id',
                                     build_related_entities=True))

        # Get StateSupervisionViolationResponses
        supervision_violation_responses = \
            (p
             | 'Load SupervisionViolationResponses' >>
             BuildRootEntity(
                 dataset=query_dataset,
                 data_dict=None,
                 root_schema_class=schema.StateSupervisionViolationResponse,
                 root_entity_class=entities.StateSupervisionViolationResponse,
                 unifying_id_field='person_id',
                 build_related_entities=True
             ))

        # Get StateSupervisionPeriods
        supervision_periods = (p
                               | 'Load SupervisionPeriods' >>
                               BuildRootEntity(
                                   dataset=query_dataset,
                                   data_dict=None,
                                   root_schema_class=
                                   schema.StateSupervisionPeriod,
                                   root_entity_class=
                                   entities.StateSupervisionPeriod,
                                   unifying_id_field='person_id',
                                   build_related_entities=False))

        # Group StateIncarcerationPeriods and StateSupervisionViolationResponses
        # by person_id
        incarceration_periods_and_violation_responses = (
            {'incarceration_periods': incarceration_periods,
             'violation_responses': supervision_violation_responses}
            | 'Group StateIncarcerationPeriods to '
              'StateSupervisionViolationResponses' >>
            beam.CoGroupByKey()
        )

        # Set the fully hydrated StateSupervisionViolationResponse entities on
        # the corresponding StateIncarcerationPeriods
        incarceration_periods_with_source_violations = (
            incarceration_periods_and_violation_responses
            | 'Set hydrated StateSupervisionViolationResponses on '
            'the StateIncarcerationPeriods' >>
            beam.ParDo(SetViolationResponseOnIncarcerationPeriod()))

        # Group each StatePerson with their StateIncarcerationPeriods and
        # StateSupervisionPeriods
        person_and_periods = (
            {'person': persons,
             'incarceration_periods':
                 incarceration_periods_with_source_violations,
             'supervision_periods':
                 supervision_periods
             }
            | 'Group StatePerson to StateIncarcerationPeriods and'
              ' StateSupervisionPeriods' >>
            beam.CoGroupByKey()
        )

        # Identify SupervisionMonths from the StatePerson's
        # StateSupervisionPeriods and StateIncarcerationPeriods
        person_months = (
            person_and_periods |
            'Get Supervision Months' >>
            GetSupervisionMonths())

        # Get dimensions to include and methodologies to use
        inclusions, _ = dimensions_and_methodologies(known_args)

        # Get pipeline job details for accessing job_id
        all_pipeline_options = pipeline_options.get_all_options()

        # Add timestamp for local jobs
        job_timestamp = datetime.datetime.now().strftime('%Y-%m-%d_%H_%M_%S.%f')
        all_pipeline_options['job_timestamp'] = job_timestamp

        # Get supervision metrics
        supervision_metrics = (person_months
                               | 'Get Supervision Metrics' >>
                               GetSupervisionMetrics(
                                   pipeline_options=all_pipeline_options,
                                   inclusions=inclusions))

        # Convert the metrics into a format that's writable to BQ
        writable_metrics = (supervision_metrics
                            | 'Convert to dict to be written to BQ' >>
                            beam.ParDo(
                                SupervisionMetricWritableDict()).with_outputs(
                                    'populations'))

        # Write the metrics to the output tables in BigQuery
        populations_table = known_args.output + \
            '.supervision_population_metrics'

        _ = (writable_metrics.populations
             | f"Write population metrics to BQ table: {populations_table}" >>
             beam.io.WriteToBigQuery(
                 table=populations_table,
                 create_disposition=beam.io.BigQueryDisposition.CREATE_NEVER,
                 write_disposition=beam.io.BigQueryDisposition.WRITE_APPEND
             ))


if __name__ == '__main__':
    logging.getLogger().setLevel(logging.INFO)
    run()