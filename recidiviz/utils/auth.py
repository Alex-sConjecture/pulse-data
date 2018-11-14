# Recidiviz - a platform for tracking granular recidivism metrics in real time
# Copyright (C) 2018 Recidiviz, Inc.
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

"""Tools for handling authentication of requests."""


import logging
from functools import wraps
from google.appengine.api import app_identity
from google.appengine.api import users
from flask import redirect, request


def authenticate_request(func):
    """Decorator to validate inbound request is authorized for Recidiviz

    Decorator function that checks for end-user authentication or that the
    request came from our app prior to calling the function it's decorating.

    Args:
        func: Function being decorated and its args

    Returns:
        If authenticated, results from the decorated function.
        If not, redirects to user login page
    """

    @wraps(func)
    def auth_and_call(*args, **kwargs):
        """Authenticates the inbound request and delegates.

        Args:
            *args: args to the function
            **kwargs: keyword args to the function

        Returns:
            The output of the function, if successfully authenticated.
            An error or redirect response, otherwise.
        """
        # Check this is either an admin user
        # or a call from within the app itself
        user = users.get_current_user()

        this_app_id = app_identity.get_application_id()
        incoming_app_id = request.headers.get('X-Appengine-Inbound-Appid', None)

        is_cron = request.headers.get('X-Appengine-Cron', None)

        is_task = request.headers.get('X-AppEngine-QueueName', None)

        if is_cron:
            logging.info("Requester is one of our cron jobs, proceeding.")

        elif is_task:
            logging.info("Requester is the taskqueue, proceeding.")

        elif incoming_app_id:
            # Check whether this is an intra-app call from our GAE service
            logging.info("Requester authenticated as app-id: %s." %
                         incoming_app_id)

            if incoming_app_id == this_app_id:
                logging.info("Authenticated intra-app call, proceeding.")
            else:
                logging.info("App ID is %s, not allowed - exiting."
                             % incoming_app_id)
                return ("Failed: Unauthorized external request.", 401)

        elif user:
            # Not an intra-app call, but was sent by an authenticated user.
            # Check if they're an admin / have permission to impact scrapers.
            logging.info("Requester authenticated as %s (%s)." %
                         (user.nickname(), user.email()))

            if users.is_current_user_admin():
                logging.info("Authenticated as admin, proceeding.")
            else:
                logging.info("Logged in, but not as admin - exiting.")
                return ("Failed: Not an admin.", 401)
        else:
            # No app ID, no signed-in user account - redirect to login
            return redirect(users.create_login_url(request.url))

        # If we made it this far, client is authorized - run the decorated func
        return func(*args, **kwargs)

    return auth_and_call
