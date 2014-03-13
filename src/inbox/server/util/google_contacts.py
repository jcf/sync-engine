"""Provide Google contacts."""

import dateutil.parser
import datetime
import posixpath
import time

import gdata.auth
import gdata.contacts.client

from ..models import session_scope
from ..models.tables import Contact, ImapAccount
from ..oauth import INSTALLED_CLIENT_ID, INSTALLED_CLIENT_SECRET, OAUTH_SCOPE
from ..pool import verify_imap_account
from ..log import configure_logging

SOURCE_APP_NAME = 'InboxApp Contact Sync Engine'


class GoogleContactsProvider(object):
    """A utility class to fetch and parse Google contact data for the specified
    account using the Google Contacts API.

    Parameters
    ----------
    db_session: sqlalchemy.orm.session.Session
        Database session.

    account: ..models.tables.ImapAccount
        The user account for which to fetch contact data.

    Attributes
    ----------
    google_client: gdata.contacts.client.ContactsClient
        Google API client to do the actual data fetching.
    log: logging.Logger
        Logging handler.
    """
    def __init__(self, account_id):
        self.account_id = account_id
        self.log = configure_logging(account_id, 'googlecontacts')

    def _get_google_client(self):
        """Return the Google API client."""
        # TODO(emfree) figure out a better strategy for refreshing OAuth
        # credentials as needed
        with session_scope() as db_session:
            try:
                account = db_session.query(ImapAccount).get(self.account_id)
                account = verify_imap_account(db_session, account)
                two_legged_oauth_token = gdata.gauth.OAuth2Token(
                    client_id=INSTALLED_CLIENT_ID,
                    client_secret=INSTALLED_CLIENT_SECRET,
                    scope=OAUTH_SCOPE,
                    user_agent=SOURCE_APP_NAME,
                    access_token=account.o_access_token,
                    refresh_token=account.o_refresh_token)
                google_client = gdata.contacts.client.ContactsClient(
                    source=SOURCE_APP_NAME)
                google_client.auth_token = two_legged_oauth_token
                return google_client
            except gdata.client.BadAuthentication:
                self.log.error('Invalid user credentials given')
                return None

    def _parse_contact_result(self, google_contact):
        """Constructs a Contact object from a Google contact entry.

        Parameters
        ----------
        google_contact: gdata.contacts.entry.ContactEntry
            The Google contact entry to parse.

        Returns
        -------
        ..models.tables.Contact
            A corresponding Inbox Contact instance.

        Raises
        ------
        AttributeError
           If the contact data could not be parsed correctly.
        """
        email_addresses = [email for email in google_contact.email if
                           email.primary]
        if email_addresses and len(email_addresses) > 1:
            self.log.error("Should not have more than one email per entry! {0}"
                    .format(email_addresses))
        try:
            # The id.text field of a ContactEntry object takes the form
            # 'http://www.google.com/m8/feeds/contacts/<useremail>/base/<uid>'.
            # We only want the <uid> part for g_id.
            raw_google_id = google_contact.id.text
            _, g_id = posixpath.split(raw_google_id)
            name = (google_contact.name.full_name.text if (google_contact.name
                    and google_contact.name.full_name) else None)
            updated_at = (dateutil.parser.parse(google_contact.updated.text) if
                          google_contact.updated else None)
            email_address = (email_addresses[0].address if email_addresses else
                             None)
        except AttributeError, e:
            self.log.error('Something is wrong with contact: {0}'
                    .format(google_contact))
            raise e

        # TOFIX BUG
        # This rounds down the modified timestamp to not include fractional
        # seconds.  There's an open patch for the MySQLdb, but I don't think
        # it's worth adding just for this.
        # http://sourceforge.net/p/mysql-python/feature-requests/24/
        updated_at = datetime.datetime.fromtimestamp(
            time.mktime(updated_at.utctimetuple()))

        return Contact(imapaccount_id=self.account_id, source='remote',
                       g_id=g_id, name=name, updated_at=updated_at,
                       email_address=email_address)

    def get_contacts(self, sync_from_time=None, max_results=0):
        """Fetches and parses fresh contact data.

        Parameters
        ----------
        sync_from_time: str, optional
            A time in ISO 8601 format: If not None, fetch data for contacts
            that have been updated since this time. Otherwise fetch all contact
            data.
        max_results: int, optional
            If nonzero, the maximum number of contact entries to fetch.

        Yields
        ------
        ..models.tables.Contact
            The contacts that have been updated since the last account sync.
        """
        query = gdata.contacts.client.ContactsQuery()
        # TODO(emfree): Implement batch fetching
        if max_results > 0:
            query.max_results = max_results
        query.updated_min = sync_from_time

        google_client = self._get_google_client()
        if google_client is None:
            # Return an empty generator if we couldn't create an API client
            return
        for result in google_client.GetContacts(q=query).entry:
            yield self._parse_contact_result(result)
