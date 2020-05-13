"""

A lot of the inspiration (and straight up code copying!) for this class comes from EsiPy <https://github.com/Kyria/EsiPy>
Much of the credit goes to the maintainer of that package, Kyria <tweetfleet slack: @althalus>. The reasoning for no
longer using EsiPy was due to it's reliance on pyswagger, which has caused a bit of a headache in how it operates on a
low level.

Eventually I'll rewrite this to be a bit cleaner and a bit more generic, but for now, it works!

"""

# noinspection PyPackageRequirements
from logbook import Logger
import uuid
import time
import config
import base64

import datetime
from service.const import EsiSsoMode, EsiEndpoints
from service.settings import EsiSettings, NetworkSettings
from requests import Session
from urllib.parse import urlencode, quote

pyfalog = Logger(__name__)

# todo: reimplement Caching for calls
# from esipy.cache import FileCache
# file_cache = FileCache(cache_path)
# cache_path = os.path.join(config.savePath, config.ESI_CACHE)
#
# if not os.path.exists(cache_path):
#     os.mkdir(cache_path)
#


scopes = [
    'esi-skills.read_skills.v1',
    'esi-fittings.read_fittings.v1',
    'esi-fittings.write_fittings.v1'
]


class APIException(Exception):
    """ Exception for SSO related errors """

    def __init__(self, url, code, json_response):
        self.url = url
        self.status_code = code
        self.response = json_response
        super(APIException, self).__init__(str(self))

    def __str__(self):
        if 'error' in self.response:
            return 'HTTP Error %s: %s' % (self.status_code,
                                          self.response['error'])
        elif 'message' in self.response:
            return 'HTTP Error %s: %s' % (self.status_code,
                                          self.response['message'])
        return 'HTTP Error %s' % self.status_code


class EsiAccess:
    def __init__(self):
        self.settings = EsiSettings.getInstance()

        # session request stuff
        self._session = Session()
        self._session.headers.update({
            'Accept': 'application/json',
            'Origin': 'https://esi.evepc.163.com',
            'Content-Type': 'application/x-www-form-urlencoded'
        })
        self._session.proxies = NetworkSettings.getInstance().getProxySettingsInRequestsFormat()

    @property
    def sso_url(self):
        # if self.settings.get("ssoMode") == EsiSsoMode.CUSTOM:
        return "https://login.evepc.163.com/v2"
        # return "https://www.pyfa.io"

    @property
    def esi_url(self):
        return "https://esi.evepc.163.com"

    @property
    def oauth_verify(self):
        return '%s/verify/' % self.esi_url

    @property
    def oauth_authorize(self):
        return '%s/oauth/authorize' % self.sso_url

    @property
    def oauth_token(self):
        return '%s/oauth/token' % self.sso_url

    def getSkills(self, char):
        return self.get(char, EsiEndpoints.CHAR_SKILLS.value, character_id=char.characterID)

    def getSecStatus(self, char):
        return self.get(char, EsiEndpoints.CHAR.value, character_id=char.characterID)

    def getFittings(self, char):
        return self.get(char, EsiEndpoints.CHAR_FITTINGS.value, character_id=char.characterID)

    def postFitting(self, char, json_str):
        # @todo: new fitting ID can be recovered from resp.data,
        return self.post(char, EsiEndpoints.CHAR_FITTINGS.value, json_str, character_id=char.characterID)

    def delFitting(self, char, fittingID):
        return self.delete(char, EsiEndpoints.CHAR_DEL_FIT.value, character_id=char.characterID, fitting_id=fittingID)

    @staticmethod
    def update_token(char, tokenResponse):
        """ helper function to update token data from SSO response """
        char.accessToken = tokenResponse['access_token']
        char.accessTokenExpires = datetime.datetime.fromtimestamp(time.time() + tokenResponse['expires_in'])
        if 'refresh_token' in tokenResponse:
            char.refreshToken = config.cipher.encrypt(tokenResponse['refresh_token'].encode())

    def get_device_id(self):

        baseUrl = 'https://mpay-web.g.mkey.163.com/device/init'
        args = {
            'game_id': 'aecfu6bgiuaaaal2-g-ma79',
            'device_type': 'PC',
            'system_name': 'Windows',
            'system_version': '10',
            'resolution': '1920*1080',
            'device_model': '64',
        }
        try:
            resp = self._after_request(self._session.get(baseUrl, data=args)).json()
            pyfalog.info(resp)
            print(resp)
            return resp['device']['id']
        except Exception as e:
            pyfalog.error(e)
            return ''

    def getLoginURI(self, redirect=None):
        self.state = str(uuid.uuid4())
        device_id = self.get_device_id()
        args = {
            'response_type': 'token',
            'client_id': 'bc90aa496a404724a93f41b4f4e97761',
            'redirect_uri': 'https%3A%2F%2Fesi.evepc.163.com%2Fui%2Foauth2-redirect.html',
            'scope': '+'.join(scopes) if scopes else '',
            'realm': 'ESI',
            'state': self.state,
            'device_id': device_id,
        }
        args_arr = []
        for k in args.keys():
            args_arr.append('{}={}'.format(k, args[k]))
        return '{}?{}'.format(self.oauth_authorize, '&'.join(args_arr))

    def get_oauth_header(self, token):
        """ Return the Bearer Authorization header required in oauth calls

        :return: a dict with the authorization header
        """
        return {'Authorization': 'Bearer %s' % token}

    def get_refresh_token_params(self, refreshToken):
        """ Return the param object for the post() call to get the access_token
        from the refresh_token

        :param code: the refresh token
        :return: a dict with the url, params and header
        """
        if refreshToken is None:
            raise AttributeError('No refresh token is defined.')

        data = {
            'grant_type': 'refresh_token',
            'refresh_token': refreshToken,
        }

        if self.settings.get('ssoMode') == EsiSsoMode.AUTO:
            # data is all we really need, the rest is handled automatically by pyfa.io
            return {
                'data': data,
                'url': self.oauth_token,
            }

        # otherwise, we need to make the token with the client keys
        return self.__make_token_request_parameters(data)

    def __get_token_auth_header(self):
        """ Return the Basic Authorization header required to get the tokens

        :return: a dict with the headers
        """
        # encode/decode for py2/py3 compatibility
        auth_b64 = "%s:%s" % (self.settings.get('clientID'), self.settings.get('clientSecret'))
        auth_b64 = base64.b64encode(auth_b64.encode('latin-1'))
        auth_b64 = auth_b64.decode('latin-1')

        return {'Authorization': 'Basic %s' % auth_b64}

    def __make_token_request_parameters(self, params):
        request_params = {
            'headers': self.__get_token_auth_header(),
            'data': params,
            'url': self.oauth_token,
        }

        return request_params

    def get_access_token_request_params(self, code):
        return self.__make_token_request_parameters(
            {
                'grant_type': 'authorization_code',
                'code': code,
            }
        )

    def auth(self, code):
        request_data = self.get_access_token_request_params(code)
        res = self._session.post(**request_data)
        if res.status_code != 200:
            raise Exception(
                request_data['url'],
                res.status_code,
                res.json()
            )
        json_res = res.json()
        return json_res

    def refresh(self, ssoChar):
        request_data = self.get_refresh_token_params(config.cipher.decrypt(ssoChar.refreshToken).decode())
        res = self._session.post(**request_data)
        if res.status_code != 200:
            raise APIException(
                request_data['url'],
                res.status_code,
                res.json()
            )
        json_res = res.json()
        self.update_token(ssoChar, json_res)
        return json_res

    def _before_request(self, ssoChar):
        if ssoChar.is_token_expired():
            pyfalog.info("Refreshing token for {}".format(ssoChar.characterName))
            self.refresh(ssoChar)

        if ssoChar.accessToken is not None:
            self._session.headers.update(self.get_oauth_header(ssoChar.accessToken))

    def _after_request(self, resp):
        if "warning" in resp.headers:
            pyfalog.warn("{} - {}".format(resp.headers["warning"], resp.url))

        if resp.status_code >= 400:
            raise APIException(
                resp.url,
                resp.status_code,
                resp.json()
            )

        return resp

    def get(self, ssoChar, endpoint, *args, **kwargs):
        self._before_request(ssoChar)
        endpoint = endpoint.format(**kwargs)
        return self._after_request(self._session.get("{}{}".format(self.esi_url, endpoint)))

    def post(self, ssoChar, endpoint, json, *args, **kwargs):
        self._before_request(ssoChar)
        endpoint = endpoint.format(**kwargs)
        return self._after_request(self._session.post("{}{}".format(self.esi_url, endpoint), data=json))

    def delete(self, ssoChar, endpoint, *args, **kwargs):
        self._before_request(ssoChar)
        endpoint = endpoint.format(**kwargs)
        return self._after_request(self._session.delete("{}{}".format(self.esi_url, endpoint)))
