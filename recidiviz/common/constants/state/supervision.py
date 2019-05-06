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

"""Shared constants related to supervision."""
import recidiviz.common.constants.state.enum_canonical_strings as \
    state_enum_strings
from recidiviz.common.constants.entity_enum import EntityEnum, EntityEnumMeta


# TODO(1697): Revisit these values! What do all of them mean?
class SupervisionType(EntityEnum, metaclass=EntityEnumMeta):
    HALFWAY_HOUSE = state_enum_strings.supervision_type_halfway_house
    PAROLE = state_enum_strings.supervision_type_parole
    POST_CONFINEMENT = state_enum_strings.supervision_type_post_confinement
    PRE_CONFINEMENT = state_enum_strings.supervision_type_pre_confinement
    PROBATION = state_enum_strings.supervision_type_probation

    @staticmethod
    def _get_default_map():
        return _SUPERVISION_TYPE_MAP


_SUPERVISION_TYPE_MAP = {
    'HALFWAY HOUSE': SupervisionType.HALFWAY_HOUSE,
    'HALFWAY HOME': SupervisionType.HALFWAY_HOUSE,
    'HALFWAY': SupervisionType.HALFWAY_HOUSE,
    'PAROLE': SupervisionType.PAROLE,
    'POST CONFINEMENT': SupervisionType.POST_CONFINEMENT,
    'POST RELEASE': SupervisionType.POST_CONFINEMENT,
    'PRE CONFINEMENT': SupervisionType.PRE_CONFINEMENT,
    'PRE RELEASE': SupervisionType.PRE_CONFINEMENT,
    'PROBATION': SupervisionType.PROBATION,
}
