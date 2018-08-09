"""
OAuth2 (RFC6749) implementation, using :mod:`authlib`.

This module extends the :mod:`authlib.flask` implementation, leveraging client
data stored in :mod:`registry.services.datastore` and instantiating authorized
sessions in :mod:`registry.services.sessions`.

The current implementation supports only the `client_credentials` grant.

.. todo:: Implement the `authorization_code` and `password` grants.


"""

from typing import List, Optional
import hashlib
from flask import Request, Flask, current_app, request
from authlib.flask.oauth2 import AuthorizationServer
from authlib.specs.rfc6749 import ClientMixin, grants, OAuth2Request

from arxiv.base.globals import get_application_config, get_application_global
from arxiv.base import logging
from ..services import datastore, sessions
from .. import domain

logger = logging.getLogger(__name__)


class OAuth2Client(ClientMixin):
    """
    Implementation of an OAuth2 client as described in RFC6749.

    This class essentially wraps an aggregate of registry domain objects for a
    particular client, and implements methods expected by the
    :class:`AuthorizationServer`.

    """

    def __init__(self, client: domain.Client,
                 credential: domain.ClientCredential,
                 authorizations: List[domain.ClientAuthorization],
                 grant_types: List[domain.ClientGrantType]) -> None:
        """Initialize with domain data about a client."""
        logger.debug('New OAuth2Client with client_id %s', client.client_id)
        self._client = client
        self._credential = credential
        self._scopes = set([auth.scope for auth in authorizations])
        self._grant_types = [gtype.grant_type for gtype in grant_types]

    @property
    def scopes(self) -> List[str]:
        """Authorized scopes as a list."""
        return list(self._scopes)

    def check_client_secret(self, client_secret: str) -> bool:
        """Check that the provided client secret is correct."""
        logger.debug('Check client secret %s', client_secret)
        hashed = hashlib.sha256(client_secret.encode('utf-8')).hexdigest()
        return self._credential.client_secret == hashed

    def check_grant_type(self, grant_type: str) -> bool:
        """Check that the client is authorized for the proposed grant type."""
        logger.debug('Check grant type %s', grant_type)
        return grant_type in self._grant_types

    def check_redirect_uri(self, redirect_uri: str) -> bool:
        """Check that the provided redirect URI is authorized."""
        return redirect_uri == self._client.redirect_uri

    def check_requested_scopes(self, scopes: List[str]) -> bool:
        """Check that the requested scopes are authorized for this client."""
        return set(self._scopes).issuperset(scopes)

    def check_response_type(self, response_type: str) -> bool:
        """Check the proposed response type."""
        return response_type == 'code'

    def check_token_endpoint_auth_method(self, method: str) -> bool:
        """Force POST auth method."""
        return method == 'client_secret_post'

    def get_default_redirect_uri(self) -> str:
        """Get the default redirect URI for the client."""
        return self._client.redirect_uri

    def has_client_secret(self) -> bool:
        """Check that the client has a secret."""
        return self._credential.client_secret is not None


class ClientCredentialsGrant(grants.ClientCredentialsGrant):
    """Our client credentials grant supports only POST requests."""

    TOKEN_ENDPOINT_AUTH_METHODS = ['client_secret_post']


def get_client(client_id: str) -> Optional[OAuth2Client]:
    """
    Load client data and generate a :class:`OAuth2Client`.

    Parameters
    ----------
    client_id : str

    Returns
    -------
    :class:`OAuth2Client` or None
        If the client is not found, returns `None`.

    """
    logger.debug('Get client with ID %s', client_id)
    try:
        client = OAuth2Client(*datastore.load_client(client_id))
        logger.debug('Got client %s', client_id)
    except datastore.NoSuchClient as e:
        logger.debug('No such client %s: %s', client_id, e)
        return None
    return client


def save_token(token: dict, oauth_request: OAuth2Request) -> None:
    """
    Persist an auth token as a :class:`domain.Session`.

    We use the access token as the session ID. This makes for a fast lookup
    by the :mod:`authenticator` service.

    Parameters
    ----------
    token : dict
        Token data generated by the OAuth2 :class:`AuthorizationServer`.
        At this point the token has not been stored.
    oauth_request : :class:`OAuth2Request`
        Wrapper for OAuth2-related request data.

    """
    logger.debug("Persist token: %s", token)
    session_id = token['access_token']
    client = oauth_request.client
    logger.debug("Client has scopes %s", client.scopes)
    user = oauth_request.user if oauth_request.user else None
    authorizations = domain.Authorizations(scopes=client.scopes)
    session = sessions.create(authorizations, request.remote_addr,
                              request.remote_addr, user=user,
                              client=client._client, session_id=session_id)
    logger.debug('Created session %s', session.session_id)


def create_server() -> AuthorizationServer:
    """Instantiate and configure an :class:`AuthorizationServer`."""
    server = AuthorizationServer(query_client=get_client,
                                 save_token=save_token)
    server.register_grant(ClientCredentialsGrant)
    logger.debug('Created server %s', id(server))
    return server


def init_app(app: Flask) -> None:
    """Attach an :class:`AuthorizationServer` to a :class:`Flask` app."""
    server = create_server()
    server.init_app(app)
    app.server = server