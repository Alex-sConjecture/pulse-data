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

"""Calculates recidivism metrics from release events.

This contains the core logic for calculating recidivism metrics on a
person-by-person basis. It transforms ReleaseEvents into recidivism metrics,
key-value pairs where the key represents all of the dimensions represented in
the data point, and the value represents a recidivism value, e.g. 0 for no or 1
for yes.

Attributes:
    FOLLOW_UP_PERIODS: a list of integers, the follow-up periods that we measure
        recidivism over, from 1 to 10.
"""
import logging
from typing import Any, Dict, List, Optional, Tuple

from copy import deepcopy
import datetime
from datetime import date
from dateutil.relativedelta import relativedelta

from recidiviz.calculator.pipeline.recidivism.metrics import \
    ReincarcerationRecidivismMetricType
from recidiviz.calculator.pipeline.recidivism.release_event import \
    ReleaseEvent, RecidivismReleaseEvent, NonRecidivismReleaseEvent, \
    ReincarcerationReturnType, ReincarcerationReturnFromSupervisionType
from recidiviz.calculator.pipeline.utils.metric_utils import \
    MetricMethodologyType
from recidiviz.calculator.pipeline.utils.calculator_utils import age_at_date, \
    age_bucket, for_characteristics_races_ethnicities, for_characteristics, \
    augment_combination, last_day_of_month, relevant_metric_periods, first_day_of_month, \
    characteristics_with_person_id_fields
from recidiviz.common.constants.state.state_supervision_violation import \
    StateSupervisionViolationType
from recidiviz.persistence.entity.state.entities import StatePerson

# We measure in 1-year follow up periods up to 10 years after date of release.
FOLLOW_UP_PERIODS = range(1, 11)


def map_recidivism_combinations(person: StatePerson,
                                release_events:
                                Dict[int, List[ReleaseEvent]],
                                inclusions: Dict[str, bool]) \
        -> List[Tuple[Dict[str, Any], Any]]:
    """Transforms ReleaseEvents and a StatePerson into metric combinations.

    Takes in a StatePerson and all of her ReleaseEvents and returns an array
    of "recidivism combinations". These are key-value pairs where the key
    represents a specific metric and the value represents whether or not
    recidivism occurred.

    This translates a particular recidivism event into many different recidivism
    metrics. Both count-based and rate-based metrics are generated. Each metric
    represents one of many possible combinations of characteristics being
    tracked for that event. For example, if an asian male is reincarcerated,
    there is a metric that corresponds to asian people, one to males,
    one to asian males, one to all people, and more depending on other
    dimensions in the data.

    If a release does not count towards recidivism, then the value is 0 for
    the rate-based metrics in either methodology.

    For both count and rate-based metrics, the value is 0 if the dimensions
    of the metric do not fully match the attributes of the person and their type
    of return to incarceration. For example, for a RecidivismReleaseEvent where
    the return_type is 'REVOCATION', there will be metrics produced where the
    return_type is 'NEW ADMISSION' and the value is 0.

    Args:
        person: the StatePerson
        release_events: A dictionary mapping release cohorts to a list of
            ReleaseEvents for the given StatePerson.
        inclusions: A dictionary containing the following keys that correspond
            to characteristic dimensions:
                - age_bucket
                - ethnicity
                - gender
                - race
                - release_facility
                - stay_length_bucket
            Where the values are boolean flags indicating whether to include
            the dimension in the calculations.
    Returns:
        A list of key-value tuples representing specific metric combinations and
        the recidivism value corresponding to that metric.
    """
    metrics = []
    all_reincarcerations = reincarcerations(release_events)

    metric_period_end_date = last_day_of_month(date.today())

    for release_cohort, events in release_events.items():
        for event in events:
            characteristic_combos_rates = \
                characteristic_combinations(person, event, inclusions)
            characteristic_combos_counts = deepcopy(
                characteristic_combos_rates)
            characteristic_combos_liberty = deepcopy(
                characteristic_combos_rates)

            rate_metrics = map_recidivism_rate_combinations(
                characteristic_combos_rates, release_cohort, event,
                release_events, all_reincarcerations)

            metrics.extend(rate_metrics)

            count_metrics = \
                map_recidivism_count_combinations(characteristic_combos_counts,
                                                  event,
                                                  all_reincarcerations,
                                                  metric_period_end_date)

            metrics.extend(count_metrics)

            liberty_metrics = \
                map_recidivism_liberty_combinations(
                    characteristic_combos_liberty, event, all_reincarcerations)

            metrics.extend(liberty_metrics)

    return metrics


def map_recidivism_rate_combinations(
        characteristic_combos: List[Dict[str, Any]],
        release_cohort,
        event: ReleaseEvent,
        all_release_events: Dict[int, List[ReleaseEvent]],
        all_reincarcerations: Dict[date, Dict[str, Any]]) -> \
        List[Tuple[Dict[str, Any], Any]]:
    """Maps the given event and characteristic combinations to a variety of
    metrics that track rate-based recidivism.

    Args:
        characteristic_combos: A list of dictionaries containing all unique
            combinations of characteristics.
        release_cohort: The year the person was released from the previous
            period of incarceration.
        event: the recidivism event from which the combination was derived
        all_release_events: A dictionary mapping release cohorts to a list of
            ReleaseEvents for the given StatePerson.
        all_reincarcerations: dictionary where the keys are all dates of
            reincarceration for the person's ReleaseEvents, and the values
            are a dictionary containing return type and from supervision type
            information

    Returns:
        A list of key-value tuples representing specific metric combinations and
        the recidivism value corresponding to that metric.
    """
    metrics = []

    if isinstance(event, RecidivismReleaseEvent):
        earliest_recidivism_period = earliest_recidivated_follow_up_period(
            event.release_date, event.reincarceration_date)
    else:
        earliest_recidivism_period = None

    relevant_periods = relevant_follow_up_periods(event.release_date, date.today(), FOLLOW_UP_PERIODS)

    for combo in characteristic_combos:
        # Don't calculate person-level recidivism rate metrics
        if combo.get('person_id') is None:
            combo['metric_type'] = ReincarcerationRecidivismMetricType.RATE
            combo['release_cohort'] = release_cohort

            metrics.extend(combination_rate_metrics(
                combo, event, all_release_events, all_reincarcerations,
                earliest_recidivism_period, relevant_periods))

    return metrics


def map_recidivism_count_combinations(
        characteristic_combos: List[Dict[str, Any]],
        event: ReleaseEvent,
        all_reincarcerations: Dict[date, Dict[str, Any]],
        metric_period_end_date: date) -> \
        List[Tuple[Dict[str, Any], Any]]:
    """Maps the given event and characteristic combinations to a variety of metrics that track count-based recidivism.

    If the event is a RecidivismReleaseEvent, then a count of reincarceration occurred. This produces metrics for both
    the year and the month in which the person was reincarcerated.

    Args:
        characteristic_combos: A list of dictionaries containing all unique combinations of characteristics.
        event: the recidivism event from which the combination was derived
        all_reincarcerations: dictionary where the keys are all dates of reincarceration for the person's ReleaseEvents,
            and the values are a dictionary containing return type and from supervision type information
        metric_period_end_date: The day the metric periods end

    Returns:
        A list of key-value tuples representing specific metric combinations and the recidivism value corresponding to
            that metric.
    """
    metrics = []

    if isinstance(event, RecidivismReleaseEvent):
        reincarceration_date = event.reincarceration_date

        relevant_periods = relevant_metric_periods(reincarceration_date,
                                                   metric_period_end_date.year,
                                                   metric_period_end_date.month)

        for combo in characteristic_combos:
            combo['metric_type'] = ReincarcerationRecidivismMetricType.COUNT

            # Bucket for the month of the incarceration
            combo['year'] = reincarceration_date.year
            combo['month'] = reincarceration_date.month
            combo['metric_period_months'] = 1

            end_of_event_month = last_day_of_month(reincarceration_date)

            metrics.extend(combination_count_metrics(combo, event, all_reincarcerations, end_of_event_month))

            # Bucket for each of the relevant metric period month lengths
            for relevant_period in relevant_periods:
                combo['year'] = metric_period_end_date.year
                combo['month'] = metric_period_end_date.month
                combo['metric_period_months'] = relevant_period

                metrics.extend(combination_count_metrics(combo, event, all_reincarcerations, metric_period_end_date))

    return metrics


def map_recidivism_liberty_combinations(
        characteristic_combos: List[Dict[str, Any]],
        event: ReleaseEvent,
        all_reincarcerations: Dict[date, Dict[str, Any]]) -> \
        List[Tuple[Dict[str, Any], Any]]:
    """Maps the given event and characteristic combinations to a variety of metrics that track metrics for time at
    liberty.
    """
    metrics = []

    if isinstance(event, RecidivismReleaseEvent):
        reincarceration_date = event.reincarceration_date

        year = reincarceration_date.year
        year_start_day = date(year, 1, 1)
        year_end_day = date(year, 12, 31)
        month_start_day = first_day_of_month(reincarceration_date)
        month_end_day = last_day_of_month(reincarceration_date)

        for combo in characteristic_combos:
            combo['metric_type'] = ReincarcerationRecidivismMetricType.LIBERTY

            # Year bucket
            combo['start_date'] = year_start_day
            combo['end_date'] = year_end_day

            metrics.extend(combination_liberty_metrics(
                combo, event, all_reincarcerations))

            # Month bucket
            combo['start_date'] = month_start_day
            combo['end_date'] = month_end_day

            metrics.extend(combination_liberty_metrics(combo, event, all_reincarcerations))

    return metrics


def reincarcerations(release_events: Dict[int, List[ReleaseEvent]]) \
        -> Dict[date, Dict[str, Any]]:
    """Finds the reincarcerations within the given ReleaseEvents.

    Returns a dictionary where the keys are all dates of reincarceration for
    the person's ReleaseEvents, and the values are a dictionary containing
    return type and from supervision type information.

    If one of the given events is not an instance of
    recidivism, i.e. it is not a RecidivismReleaseEvent, then it is not
    represented in the output.

    Args:
        release_events: the list of ReleaseEvents.

    Returns:
        A dictionary representing the dates of reincarceration and the return
        descriptors for each reincarceration.
    """
    reincarcerations_dict: Dict[date, Dict[str, Any]] = {}

    for _cohort, events in release_events.items():
        for event in events:
            if isinstance(event, RecidivismReleaseEvent):
                reincarcerations_dict[event.reincarceration_date] = \
                    {'release_date': event.release_date,
                     'return_type': event.return_type,
                     'from_supervision_type': event.from_supervision_type,
                     'source_violation_type': event.source_violation_type}

    return reincarcerations_dict


def reincarcerations_in_window(start_date: date,
                               end_date: date,
                               all_reincarcerations:
                               Dict[date, Dict[str, Any]]) \
        -> List[Dict[str, Any]]:
    """Finds the number of reincarceration dates during the given window.

    Returns how many of the given reincarceration dates fall within the given
    follow-up period after the given start date, end point exclusive, including
    the start date itself if it is within the given array.

    Example:
        count_reincarcerations_in_window("2016-05-13", 6,
            {"2012-04-30": {'return_type': 'NEW_ADMISSION',
                            'from_supervision_type': None},
             "2016-05-13": {'return_type': 'REVOCATION',
                            'from_supervision_type': 'PAROLE'},
             "2020-11-20": {'return_type': 'NEW_ADMISSION',
                            'from_supervision_type': None},
             "2021-01-12": {'return_type': 'REVOCATION',
                            'from_supervision_type': 'PROBATION'},
             "2022-05-13": {'return_type': 'NEW_ADMISSION',
                            'from_supervision_type': None}
             }) = 3

    Args:
        start_date: a Date to start tracking from
        end_date: a Date to stop tracking
        all_reincarcerations: the dictionary of reincarcerations to check

    Returns:
        How many of the given reincarcerations are within the window
        specified by the given start date and end date.
    """
    reincarcerations_in_window_dict = \
        [reincarceration for reincarceration_date, reincarceration
         in all_reincarcerations.items()
         if end_date > reincarceration_date >= start_date]

    return reincarcerations_in_window_dict


def returned_within_follow_up_period(event: ReleaseEvent, period: int) -> bool:
    """Returns whether someone was reincarcerated within the given follow-up
    period following their release."""
    start_date = event.release_date
    end_date = start_date + relativedelta(years=period)

    if isinstance(event, RecidivismReleaseEvent):
        return start_date <= event.reincarceration_date < end_date

    return False


def days_at_liberty(event: ReleaseEvent) -> int:
    """Returns the number of days between a release and a reincarceration.

    If the event is not an instance of recidivism, returns -1.
    """
    release_date = event.release_date

    if isinstance(event, RecidivismReleaseEvent):
        return_date = event.reincarceration_date

        delta = return_date - release_date
        return delta.days

    return -1


def earliest_recidivated_follow_up_period(
        release_date: date,
        reincarceration_date: Optional[date]) -> Optional[int]:
    """Finds the earliest follow-up period under which recidivism has occurred.

    For example, if someone was released from prison on March 14, 2005 and
    reincarcerated on April 23, 2008, then the earliest follow-up period is 4,
    as they had not yet recidivated within 3 years, but had within 4.

    Args:
        release_date: a Date for when the person was released
        reincarceration_date: a Date for when the person was reincarcerated

    Returns:
        An integer for the earliest follow-up period under which recidivism
        occurred. None if there is no reincarceration date provided.
    """
    if not reincarceration_date:
        return None

    years_apart = reincarceration_date.year - release_date.year

    if years_apart == 0:
        return 1

    after_anniversary = ((reincarceration_date.month, reincarceration_date.day)
                         > (release_date.month, release_date.day))
    return years_apart + 1 if after_anniversary else years_apart


def relevant_follow_up_periods(release_date: date, current_date: date,
                               follow_up_periods: range) -> List[int]:
    """Finds the given follow-up periods which are relevant to measurement.

    Returns all of the given follow-up periods after the given release date
    which are either complete as the current_date, or still in progress as of
    today.

    Examples where today is 2018-01-26:
        relevant_follow_up_periods("2015-01-05", today, FOLLOW_UP_PERIODS) =
            [1,2,3,4]
        relevant_follow_up_periods("2015-01-26", today, FOLLOW_UP_PERIODS) =
            [1,2,3,4]
        relevant_follow_up_periods("2015-01-27", today, FOLLOW_UP_PERIODS) =
            [1,2,3]
        relevant_follow_up_periods("2016-01-05", today, FOLLOW_UP_PERIODS) =
            [1,2,3]
        relevant_follow_up_periods("2017-04-10", today, FOLLOW_UP_PERIODS) =
            [1]
        relevant_follow_up_periods("2018-01-05", today, FOLLOW_UP_PERIODS) =
            [1]
        relevant_follow_up_periods("2018-02-05", today, FOLLOW_UP_PERIODS) =
            []

    Args:
        release_date: the release Date we are tracking from
        current_date: the current Date we are tracking towards
        follow_up_periods: the list of follow up periods to filter

    Returns:
        The list of follow up periods which are relevant to measure, i.e.
        already completed or still in progress.
    """
    return [period for period in follow_up_periods
            if release_date + relativedelta(years=period - 1) <= current_date]


def stay_length_from_event(event: ReleaseEvent) -> Optional[int]:
    """Calculates the length of facility stay of a given event in months.

    This is rounded down to the nearest month, so a stay from 2015-01-15 to
    2017-01-14 results in a stay length of 23 months. Note that bucketing in
    stay_length_bucketing is upper bound exclusive, so in this example the
    bucket would be 12-24, and if the stay ended on 2017-01-15, the stay length
    would be 24 months and the bucket would be 24-36.

    Args:
        event: the ReleaseEvent

    Returns:
        The length of the facility stay in months. None if the original
        admission date or release date is not known.
    """
    if event.original_admission_date is None or event.release_date is None:
        return None

    delta = relativedelta(event.release_date, event.original_admission_date)
    return delta.years * 12 + delta.months


def stay_length_bucket(stay_length: Optional[int]) -> Optional[str]:
    """Calculates the stay length bucket that applies to measurement.

    Stay length buckets (upper bound exclusive) for measurement:
        <12, 12-24, 24-36, 36-48, 48-60, 60-72,
        72-84, 84-96, 96-108, 108-120, 120+.

    Args:
        stay_length: the length in months of the person's facility stay.

    Returns:
        A string representation of the age bucket for the person.
    """
    if stay_length is None:
        return None
    if stay_length < 12:
        return '<12'
    if stay_length < 24:
        return '12-24'
    if stay_length < 36:
        return '24-36'
    if stay_length < 48:
        return '36-48'
    if stay_length < 60:
        return '48-60'
    if stay_length < 72:
        return '60-72'
    if stay_length < 84:
        return '72-84'
    if stay_length < 96:
        return '84-96'
    if stay_length < 108:
        return '96-108'
    if stay_length < 120:
        return '108-120'
    return '120<'


def characteristic_combinations(person: StatePerson,
                                event: ReleaseEvent,
                                inclusions: Dict[str, bool]) -> \
        List[Dict[str, Any]]:
    """Calculates all recidivism metric combinations.

    Returns the list of all combinations of the metric characteristics, of all sizes, given the StatePerson and
    ReleaseEvent. That is, this returns a list of dictionaries where each dictionary is a combination of 0 to n unique
    elements of characteristics, where n is the size of the given array.

    For each event, we need to calculate metrics across combinations of:
    Release Cohort; Follow-up Period (up to 10 years);
    MetricMethodologyType (Event-based, Person-based);
    Demographics (age, race, ethnicity, gender); Location (facility, region);
    Facility Stay Breakdown (stay length);
    Return Descriptors (return type, from supervision type)
    TODO: Add support for offense and sentencing type (Issues 33 and 32)

    Release cohort, follow-up period, and methodology are not included in the output here. They are added into
    augmented versions of these combinations later.

    The output for a black female age 24 and an incarceration that began in January 2008 and ended in February 2009 is
    equal to the output of:
            for_characteristics({'age': '<25', 'race': Race.BLACK,
                                 'gender': Gender.FEMALE,
                                 'stay_length_bucket': '12-24'})

    Our schema allows for a StatePerson to have more than one race. The output for a female age 24 who is both white
    and black and an incarceration that began in January 2008 and ended in February 2009 is equal to the union of the
    outputs for:

     for_characteristics({'age': '<25', 'race': Race.BLACK,
                                 'gender': Gender.FEMALE,
                                 'stay_length_bucket': '12-24'})

     and

     for_characteristics({'age': '<25', 'race': Race.WHITE,
                                 'gender': Gender.FEMALE,
                                 'stay_length_bucket': '12-24'})

    where there are no duplicate metrics in the output.

    Args:
        person: the StatePerson we are picking characteristics from
        event: the ReleaseEvent we are picking characteristics from
        inclusions: A dictionary containing the following keys that correspond to characteristic dimensions:
                - age_bucket
                - ethnicity
                - gender
                - race
                - release_facility
                - stay_length_bucket
            Where the values are boolean flags indicating whether to include the dimension in the calculations.

    Returns:
        A list of dictionaries containing all unique combinations of characteristics.
    """
    characteristics: Dict[str, Any] = {}

    if event.county_of_residence:
        characteristics['county_of_residence'] = event.county_of_residence

    if inclusions.get('age_bucket'):
        entry_age = age_at_date(person, event.original_admission_date)
        entry_age_bucket = age_bucket(entry_age)
        if entry_age_bucket is not None:
            characteristics['age_bucket'] = entry_age_bucket
    if inclusions.get('gender'):
        if person.gender is not None:
            characteristics['gender'] = person.gender
    if inclusions.get('release_facility'):
        if event.release_facility is not None:
            characteristics['release_facility'] = event.release_facility
    if inclusions.get('stay_length_bucket'):
        event_stay_length = stay_length_from_event(event)
        event_stay_length_bucket = stay_length_bucket(event_stay_length)
        characteristics['stay_length_bucket'] = event_stay_length_bucket
    if person.races or person.ethnicities:
        if inclusions.get('race'):
            races = person.races
        else:
            races = []

        if inclusions.get('ethnicity'):
            ethnicities = person.ethnicities
        else:
            ethnicities = []

        all_combinations = for_characteristics_races_ethnicities(races, ethnicities, characteristics)
    else:
        all_combinations = for_characteristics(characteristics)

    characteristics_with_person_details = characteristics_with_person_id_fields(characteristics, person, 'recidivism')

    all_combinations.append(characteristics_with_person_details)

    return all_combinations


def sorted_releases_in_year(release_date: date, all_release_events: Dict[int, List[ReleaseEvent]]) -> \
        List[ReleaseEvent]:
    """Returns the releases in a given year, sorted by release date."""
    year_of_release = release_date.year

    releases_in_year = all_release_events.get(year_of_release)

    if not releases_in_year:
        raise ValueError(f"Release year {year_of_release} should be present in release_events: {all_release_events}. "
                         f"Identifier code is not correctly classifying all release events by release cohort year.")

    releases_in_year.sort(key=lambda b: b.release_date)

    return releases_in_year


def combination_rate_metrics(combo: Dict[str, Any],
                             event: ReleaseEvent,
                             all_release_events: Dict[int, List[ReleaseEvent]],
                             all_reincarcerations: Dict[date, Dict[str, Any]],
                             earliest_recidivism_period: Optional[int],
                             relevant_periods: List[int]) -> List[Tuple[Dict[str, Any], int]]:
    """Returns all unique recidivism rate metrics for the given combination.

    For the characteristic combination, i.e. a unique metric, look at all follow-up periods to determine under which
    ones recidivism occurred. For each methodology and period, get a list of combos that are augmented with
    methodology, period, and return details. Then, map each augmented combo to 0 or 1 accordingly.

    Args:
        combo: a characteristic combination to convert into metrics
        event: the release event from which the combination was derived
        all_release_events: A dictionary mapping release cohorts to a list of ReleaseEvents for the given StatePerson.
        all_reincarcerations: dictionary where the keys are all dates of reincarceration for the person's ReleaseEvents,
            and the values are a dictionary containing return type and from supervision type information
        earliest_recidivism_period: the earliest follow-up period under which recidivism occurred
        relevant_periods: the list of periods relevant for measurement

    Returns:
        A list of key-value tuples representing specific metric combination dictionaries and the recidivism value
            corresponding to that metric.
    """
    metrics = []

    releases_in_year = sorted_releases_in_year(event.release_date, all_release_events)

    # There will always be at least one release in this list that represents the current release event.
    # `sorted_releases_in_year` should fail if that is not the case.
    if not releases_in_year:
        raise ValueError("Function `sorted_releases_in_year` should not be returning empty lists.")

    is_first_release_in_year = (id(event) == id(releases_in_year[0]))

    for period in relevant_periods:
        person_based_combos = augmented_combo_list(combo, event, MetricMethodologyType.PERSON, period)

        event_based_combos = augmented_combo_list(combo, event, MetricMethodologyType.EVENT, period)

        # If they didn't recidivate at all or not yet for this period (or they didn't recidivate until 10 years had
        # passed), assign 0 for both event- and person-based measurement.
        if isinstance(event, NonRecidivismReleaseEvent) or not earliest_recidivism_period \
                or period < earliest_recidivism_period:

            if is_first_release_in_year:
                # Only count the first release in a year for person-based metrics
                for person_combo in person_based_combos:
                    metrics.append((person_combo, 0))

            for event_combo in event_based_combos:
                metrics.append((event_combo, 0))

        # If they recidivated, each unique release of a given person within a follow-up period after the year of release
        # may be counted as an instance of recidivism for event-based measurement. For person-based measurement, only
        # one instance may be counted.
        elif isinstance(event, RecidivismReleaseEvent):
            if is_first_release_in_year:
                # Only count the first release in a year for person-based metrics
                for person_combo in person_based_combos:
                    metrics.append((person_combo, recidivism_value_for_metric(
                        person_combo, event.return_type,
                        event.from_supervision_type,
                        event.source_violation_type)))

            end_of_follow_up_period = event.release_date + relativedelta(years=period)

            all_reincarcerations_in_window = reincarcerations_in_window(event.release_date,
                                                                        end_of_follow_up_period,
                                                                        all_reincarcerations)

            for reincarceration in all_reincarcerations_in_window:
                for event_combo in event_based_combos:
                    metrics.append((event_combo, recidivism_value_for_metric(
                        event_combo,
                        reincarceration.get('return_type'),
                        reincarceration.get('from_supervision_type'),
                        reincarceration.get('source_violation_type'))))

    return metrics


def combination_count_metrics(combo: Dict[str, Any], event:
                              RecidivismReleaseEvent,
                              all_reincarcerations:
                              Dict[date, Dict[str, Any]],
                              metric_period_end_date: date) \
        -> List[Tuple[Dict[str, Any], int]]:
    """"Returns all unique recidivism count metrics for the given event and combination.

    If the event is an instance of recidivism, then for each methodology, gets a list of combos that are augmented with
    methodology and return details. Then, maps each augmented combo to 0 or 1 accordingly.

    Args:
        combo: a characteristic combination to convert into metrics
        event: the release event from which the combination was derived
        all_reincarcerations: dictionary where the keys are all dates of reincarceration for the person's ReleaseEvents,
            and the values are a dictionary containing return type and from supervision type information
        metric_period_end_date: The day the metric periods end

    Returns:
        A list of key-value tuples representing specific metric combination dictionaries and the recidivism value
            corresponding to that metric.
    """
    metrics = []

    # Each return is included for event-based measurement. However, for person-based measurement, only one instance may
    # be counted in a given window.
    person_based_combos = augmented_combo_list(combo, event, MetricMethodologyType.PERSON, None)

    event_based_combos = augmented_combo_list(combo, event, MetricMethodologyType.EVENT, None)

    # Adds one day because the reincarcerations_in_window function is exclusive of the end date, and we want the count
    # to include reincarcerations that happen on the last day of this count window.
    end_date = metric_period_end_date + datetime.timedelta(days=1)

    all_reincarcerations_in_window = reincarcerations_in_window(
        event.reincarceration_date, end_date, all_reincarcerations)

    if len(all_reincarcerations_in_window) == 1:
        # This function will be called for every single one of the person's release events that resulted in a
        # reincarceration. If this is the last instance of reincarceration before the end of the window, then include
        # this in the person-based count.
        for person_combo in person_based_combos:
            metrics.append((person_combo, recidivism_value_for_metric(
                person_combo, event.return_type,
                event.from_supervision_type,
                event.source_violation_type)))

    for event_combo in event_based_combos:
        metrics.append((event_combo, recidivism_value_for_metric(
            event_combo,
            event.return_type,
            event.from_supervision_type,
            event.source_violation_type)))

    if combo.get('person_id') is not None:
        # Only include person-level count metrics that are applicable to the person
        metrics = [(combination, value) for combination, value in metrics if value == 1]

    return metrics


def combination_liberty_metrics(combo: Dict[str, Any], event:
                                RecidivismReleaseEvent,
                                all_reincarcerations:
                                Dict[date, Dict[str, Any]]) \
        -> List[Tuple[Dict[str, Any], int]]:
    """Returns all unique recidivism liberty metrics for the given event and combination.

    For each methodology, gets a list of combos that are augmented with methodology and return details. If the event's
    return details match that of the combo, maps the augmented combo to the number of days the person was at liberty
    before being reincarcerated.

    Args:
        combo: a characteristic combination to convert into metrics
        event: the release event from which the combination was derived
        all_reincarcerations: dictionary where the keys are all dates of reincarceration for the person's ReleaseEvents,
            and the values are a dictionary containing return type and from supervision type information

    Returns:
        A list of key-value tuples representing specific metric combination dictionaries and the number of days the
            person was at liberty before being reincarcerated.
    """
    metrics: List[Tuple[Dict[str, Any], int]] = []

    time_at_liberty = days_at_liberty(event)

    if time_at_liberty < 0:
        # This should not happen, but is a safeguard against creating metrics with negative days at liberty.
        logging.info("Reincarceration date is before release date.")
        return metrics

    person_based_combos = augmented_combo_list(combo, event, MetricMethodologyType.PERSON, None)

    event_based_combos = augmented_combo_list(combo, event, MetricMethodologyType.EVENT, None)

    # Adds one day because the reincarcerations_in_window function is exclusive of the end date, and we want the count
    # to include reincarcerations that happen on the last day of this count window.
    start_date = combo['start_date']
    end_date = combo['end_date'] + datetime.timedelta(days=1)

    all_reincarcerations_in_window = reincarcerations_in_window(start_date, end_date, all_reincarcerations)

    reincarcerations_in_window_after_event = reincarcerations_in_window(event.reincarceration_date,
                                                                        end_date, all_reincarcerations)

    if len(all_reincarcerations_in_window) == len(reincarcerations_in_window_after_event):
        # This function will be called for every single one of the person's release events that resulted in a
        # reincarceration. If this is the first instance of reincarceration in the window, then include
        # this in the person-based count.
        for person_combo in person_based_combos:
            recid_value = recidivism_value_for_metric(
                person_combo, event.return_type, event.from_supervision_type,
                event.source_violation_type)

            # Only include this metric if the recidivism value for the given combination is 1, meaning the event's
            # return details matches that of the combo
            if recid_value == 1:
                metrics.append((person_combo, time_at_liberty))

    for event_combo in event_based_combos:
        recid_value = recidivism_value_for_metric(
            event_combo, event.return_type, event.from_supervision_type,
            event.source_violation_type)

        # Only include this metric if the recidivism value for the given combination is 1, meaning the event's return
        # details matches that of the combo
        if recid_value == 1:
            metrics.append((event_combo, time_at_liberty))

    return metrics


def person_level_augmented_combo(combo: Dict[str, Any], event: ReleaseEvent,
                                 methodology: MetricMethodologyType,
                                 period: Optional[int]) -> Dict[str, Any]:
    """Returns a dictionary that has been augmented with all of the parameters that apply to the given event.

    Args:
        combo: the base combo to be augmented with methodology and period
        event: the ReleaseEvent from which the combo was derived
        methodology: the MetricMethodologyType to add to each combo
        period: the follow_up_period value to add to each combo

    Returns:
        The augmented combination dictionary.
    """
    parameters: Dict[str, Any] = {'state_code': event.state_code,
                                  'methodology': methodology}

    if period:
        parameters['follow_up_period'] = period

    if isinstance(event, RecidivismReleaseEvent):
        parameters['return_type'] = event.return_type
        parameters['source_violation_type'] = event.source_violation_type
        parameters['from_supervision_type'] = event.from_supervision_type

    return augment_combination(combo, parameters)


def augmented_combo_list(combo: Dict[str, Any],
                         event: ReleaseEvent,
                         methodology: MetricMethodologyType,
                         period: Optional[int]) -> List[Dict[str, Any]]:
    """Returns a list of combo dictionaries that have been augmented with necessary parameters.

    Each combo is augmented with the given methodology and follow-up period. Then, combos are added with relevant
    pairings of return_type and from_supervision_type as well.

    Args:
        combo: the base combo to be augmented with methodology and period
        event: the ReleaseEvent from which the combo was derived
        methodology: the MetricMethodologyType to add to each combo
        period: the follow_up_period value to add to each combo

    Returns: a list of combos augmented with various parameters
    """

    if combo.get('person_id') is not None:
        return [person_level_augmented_combo(combo, event, methodology, period)]

    combos = []
    parameters: Dict[str, Any] = {'state_code': event.state_code,
                                  'methodology': methodology}

    if period:
        parameters['follow_up_period'] = period

    base_combo = augment_combination(combo, parameters)
    combos.append(base_combo)

    new_admission_parameters = parameters.copy()
    new_admission_parameters['return_type'] = ReincarcerationReturnType.NEW_ADMISSION

    combos.append(augment_combination(base_combo, new_admission_parameters))

    revocation_parameters = parameters.copy()
    revocation_parameters['return_type'] = ReincarcerationReturnType.REVOCATION
    combo_revocation = augment_combination(base_combo, revocation_parameters)
    combos.append(combo_revocation)

    for violation_type in StateSupervisionViolationType:
        violation_parameters = revocation_parameters.copy()
        violation_parameters['source_violation_type'] = violation_type
        combos.append(augment_combination(combo_revocation, violation_parameters))

    parole_parameters = revocation_parameters.copy()
    parole_parameters['from_supervision_type'] = ReincarcerationReturnFromSupervisionType.PAROLE
    parole_combo = augment_combination(combo_revocation, parole_parameters)
    combos.append(parole_combo)

    for violation_type in StateSupervisionViolationType:
        violation_parameters = parole_parameters.copy()
        violation_parameters['source_violation_type'] = violation_type
        combos.append(augment_combination(parole_combo, violation_parameters))

    probation_parameters = revocation_parameters.copy()
    probation_parameters['from_supervision_type'] = \
        ReincarcerationReturnFromSupervisionType.PROBATION
    probation_combo = augment_combination(combo_revocation, probation_parameters)
    combos.append(probation_combo)

    for violation_type in StateSupervisionViolationType:
        violation_parameters = probation_parameters.copy()
        violation_parameters['source_violation_type'] = violation_type
        combos.append(augment_combination(probation_combo, violation_parameters))

    return combos


def recidivism_value_for_metric(
        combo: Dict[str, Any],
        event_return_type:
        Optional[ReincarcerationReturnType],
        event_from_supervision_type:
        Optional[ReincarcerationReturnFromSupervisionType],
        event_source_violation_type: Optional[StateSupervisionViolationType]) \
        -> int:
    """Returns the recidivism value corresponding to the given metric combo and
    details of the return.

    Args:
        combo: metric combination
        event_return_type: the ReincarcerationReturnType of the release event
        event_from_supervision_type:
            the ReincarcerationReturnFromSupervisionType of the release event
        event_source_violation_type:
            the StateSupervisionViolationType of the violation that eventually
            resulted in this return

    Returns: 1 if the event_return_type, event_from_supervision_type, and
        event_source_violation_type match that of the combo. If the value for
        any of these fields on the combo is None, this is considered a match
        with the event's value. Returns 0 if these fields do not match.
    """
    combo_return_type = combo.get('return_type')
    combo_from_supervision_type = combo.get('from_supervision_type')
    combo_source_violation_type = combo.get('source_violation_type')

    if combo_return_type is None and combo_from_supervision_type is None and \
            combo_source_violation_type is None:
        return 1

    if combo_return_type != event_return_type:
        return 0

    if combo_return_type == \
            ReincarcerationReturnType.REVOCATION:

        if combo_from_supervision_type is None:
            if combo_source_violation_type is None or \
                    combo_source_violation_type == event_source_violation_type:
                return 1

        if combo_from_supervision_type != event_from_supervision_type:
            return 0

        if combo_source_violation_type is None:
            return 1

        if combo_source_violation_type != event_source_violation_type:
            return 0

    return 1
