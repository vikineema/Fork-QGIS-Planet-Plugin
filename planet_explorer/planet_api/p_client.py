# # -*- coding: utf-8 -*-
# """
# ***************************************************************************
#     pe_client.py
#     ---------------------
#     Date                 : August 2019
#     Copyright            : (C) 2019 Planet Inc, https://planet.com
# ***************************************************************************
# *                                                                         *
# *   This program is free software; you can redistribute it and/or modify  *
# *   it under the terms of the GNU General Public License as published by  *
# *   the Free Software Foundation; either version 2 of the License, or     *
# *   (at your option) any later version.                                   *
# *                                                                         *
# ***************************************************************************
# """
#
__author__ = "Planet Federal"
__date__ = "August 2019"
__copyright__ = "(C) 2019 Planet Inc, https://planet.com"

# This will get replaced with a git SHA1 when you do a git archive
__revision__ = "$Format:%H$"

import logging
import os
from collections.abc import Iterator
from typing import Any

import requests
from planet import Auth, Session
from planet.exceptions import InvalidAPIKey, InvalidIdentity
from planet.sync.client import Planet
from qgis.PyQt.QtCore import QObject, pyqtSignal, pyqtSlot
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..gui.pe_gui_utils import waitcursor

LOG_LEVEL = os.environ.get("PYTHON_LOG_LEVEL", "WARNING").upper()
logging.basicConfig(level=LOG_LEVEL)
log = logging.getLogger(__name__)

ITEM_ASSET_DL_REGEX = re.compile(r"^assets\.(.*):download$")
ITEM_STREAM_REGEX = re.compile(r"^webtiles?:stream$")
API_KEY_DEFAULT = "SKIP_ENVIRON"
QUOTA_URL = "https://api.planet.com/auth/v1/experimental" "/public/my/subscriptions"
TILE_SERVICE_URL = "https://tiles{0}.planet.com/data/v1/layers"


class LoginException(Exception):
    """Issues raised during client login"""

    pass


class QGISAdapter:

    _offline = False
    _message_bar_item = None

    def send(self, request: requests.PreparedRequest, **kwargs):
        error = 0
        req = QNetworkRequest(QUrl(request.url))
        for h in request.headers:
            req.setRawHeader(h.encode(), request.headers[h].encode())
        req.setRawHeader("Accept-Encoding".encode(), "gzip".encode())

        breq = QgsBlockingNetworkRequest()
        if request.method == "GET":
            error = breq.get(req)
        elif request.method == "POST":
            body = request.body
            if not isinstance(body, bytes):
                body = body.encode()
            error = breq.post(req, body)
        if error > 0:
            msg = breq.errorMessage()
            if not QGISAdapter._offline:
                QGISAdapter._offline = True
                msg_lower = msg.lower()
                if "ssl" in msg_lower or "tls" in msg_lower:
                    if "proxy" in msg_lower:
                        bar_msg = (
                            "SSL/TLS error connecting to Planet via proxy. "
                            "Your proxy may be interfering with HTTPS. "
                            "Check Settings > Options > Network."
                        )
                    else:
                        bar_msg = (
                            "SSL/TLS error connecting to Planet. If you are "
                            "using a proxy, it may be interfering with HTTPS "
                            "connections."
                        )
                elif "proxy" in msg_lower:
                    bar_msg = (
                        "Proxy connection refused. Check your proxy "
                        "settings under Settings > Options > Network."
                    )
                elif error == 2 or "timed out" in msg_lower or "timeout" in msg_lower:
                    bar_msg = (
                        "Connection to Planet timed out. The plugin will "
                        "resume automatically when connectivity is restored."
                    )
                else:
                    bar_msg = (
                        "Cannot access the internet. The plugin will resume "
                        "automatically when connectivity is restored."
                    )
                QGISAdapter._offline_msg = bar_msg
                QMetaObject.invokeMethod(
                    PlanetClient.getInstance(),
                    "_show_offline_message",
                    Qt.QueuedConnection,
                )
            if error == 1:
                raise requests.exceptions.ConnectionError(msg)
            elif error == 2:
                raise requests.exceptions.ConnectTimeout(msg)
            elif error == 3:
                raise requests.exceptions.RequestException(msg)

        if QGISAdapter._offline:
            QGISAdapter._offline = False
            QMetaObject.invokeMethod(
                PlanetClient.getInstance(),
                "_clear_offline_message",
                Qt.QueuedConnection,
            )

        content = breq.reply()
        resp = requests.Response()
        for h in content.rawHeaderList():
            header = h.data().decode()
            resp.headers[header] = content.rawHeader(h).data().decode()
        data = content.content().data()
        if resp.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
        resp._content = data
        resp.status_code = content.attribute(QNetworkRequest.HttpStatusCodeAttribute)
        return resp


class PlanetClient(QObject):
    """
    Wrapper class for ``planet`` Python package, to abstract calls and make it
    a Qt object.
    """

    loginChanged = pyqtSignal(bool)

    __instance = None

    @staticmethod
    def getInstance():
        if PlanetClient.__instance is None:
            PlanetClient()

        return PlanetClient.__instance

    def __init__(self):
        if PlanetClient.__instance is not None:
            raise Exception("Singleton class")

        QObject.__init__(self)

        PlanetClient.__instance = self

        self._user_quota = {
            "enabled": False,
            "sqkm": 0.0,
            "used": 0.0,
        }

        self._psscene_asset_types = None
        self._item_types = None
        self._bundles = None
        self._asset_types = {}
        # Login
        self._api_key = None
        self.auth = None
        self.session = None
        self.client = None
        # Base url needed for basemaps API
        self.base_url = "https://api.planet.com/"

    @pyqtSlot()
    def _show_offline_message(self):
        from ..pe_utils import PLANET_COLOR, iface

        if QGISAdapter._message_bar_item is None:
            msg = getattr(QGISAdapter, "_offline_msg", "Cannot access the internet.")
            QGISAdapter._message_bar_item = iface.messageBar().createMessage(
                "Planet Explorer",
                msg,
            )
            QGISAdapter._message_bar_item.setStyleSheet(
                "QgsMessageBarItem {{ background-color: rgb({r},{g},{b}); "
                "color: white; }}".format(
                    r=PLANET_COLOR.red(), g=PLANET_COLOR.green(), b=PLANET_COLOR.blue()
                )
            )
            iface.messageBar().pushWidget(
                QGISAdapter._message_bar_item, Qgis.Warning, 0
            )

    @pyqtSlot()
    def _clear_offline_message(self):
        import sip

        from ..pe_utils import iface

        if QGISAdapter._message_bar_item is not None:
            try:
                if not sip.isdeleted(QGISAdapter._message_bar_item):
                    iface.messageBar().popWidget(QGISAdapter._message_bar_item)
            except RuntimeError:
                pass
            QGISAdapter._message_bar_item = None

    @waitcursor
    def log_in(self, user: str, api_key: str):
        """Login using API key which can be found on
        your Account page under the My Settings tab,
        user email is not relevant at this time."""
        old_api_key = self.api_key()

        self._api_key = api_key

        # Client instance and requests sessions are instantiated
        # for logging in, because planet v3 client supports data, orders,
        # subscriptions not the basemaps API.
        # Request session is for interacting with Basemaps API
        self.auth = Auth.from_key(key=self._api_key)
        self._session = Session(auth=self.auth)
        self.mosaics_client = self._session.client("mosaics")
        self.client = Planet(session=self._session)

        self.session = requests.Session()
        self.session.auth = (self._api_key, "")
        retries = Retry(total=10, backoff_factor=1, status_forcelist=[429, 502, 503])
        # self.session.mount("https://", HTTPAdapter(max_retries=retries))
        self.session.mount("https://", QGISAdapter())

        self.validate_credentials()
        self.update_user_quota()

        if old_api_key != self.api_key():
            self.loginChanged.emit(self.has_api_key())

    def validate_credentials(self):
        log.debug("Validating API Key ...")
        try:
            for _ in self.client.data.list_searches(limit=1):
                break
            log.debug("Success! Your Planet API key is valid.")
        except (InvalidAPIKey, InvalidIdentity) as exc:
            raise LoginException from exc

    def log_out(self):
        old_api_key = self.api_key()

        # Do log out
        self.session = None
        self.client = None
        self.mosaics_client = None
        self._session = None
        self.auth = None
        self._api_key = None

        if old_api_key != self.api_key():
            self.loginChanged.emit(self.has_api_key())

    def api_key(self) -> str | None:
        """Get the API Key used to login for the current session."""
        return getattr(self, "_api_key", None)

    def has_api_key(self) -> bool:
        """Check if a user has logged in by checking existence of API key"""
        client_exists = getattr(self, "client", None) is not None
        auth_exists = getattr(self, "auth", None) is not None
        if client_exists and auth_exists:
            return self.auth.is_initialized()
        else:
            return False

    def _url(self, endpoint: str) -> str:
        """Construct a full url from the Planet API base url
        by attaching an endpoint"""
        return "{}/{}".format(self.base_url, endpoint)

    def _get(self, url: str, **params) -> dict[Any]:
        """Sends a GET request to a Planet API url"""
        response = self.session.get(url, params=params)
        response.raise_for_status()
        return response.json()

    def _consume_pages(self, endpoint: str, key: str, **params):
        """General pagination structure for Planet APIs."""
        url = self._url(endpoint)
        while True:
            response = self._get(url, **params)
            for item in response[key]:
                yield item

            if "_next" in response["_links"]:
                url = response["_links"]["_next"]
            else:
                break

    def _list(self, endpoint: str, key: str | None = None, **params) -> Iterator[Any]:
        key = key or endpoint
        for item in self._consume_pages(endpoint, key, **params):
            yield item

    def _post(self, url: str, json_data, headers=None) -> dict[Any]:
        if headers is not None:
            response = self.session.post(url, json=json_data, headers=headers)
        else:
            response = self.session.post(url, json=json_data)
        response.raise_for_status()
        return response.json()

    def _item(self, endpoint: str, **params):
        return self._get(self._url(endpoint), **params)

    def has_access_to_mosaics(self) -> bool:
        """Checks if a user (API key) has access to Basemaps Mosaics"""
        url = self._url("basemaps/v1/mosaics")
        response = self._get(url)
        return len(response["mosaics"]) > 0

    def list_mosaic_series(
        self, name_contains: str | None = None
    ) -> Iterator[dict[str, dict]]:
        """
        Iterate through all mosaic series you have access to, optionally
        filtering based on name.

        :param str name_contains:
            Search only for series that contain the specified substring in
            their names
        """
        for item in self._list(
            "basemaps/v1/series", key="series", name__contains=name_contains
        ):
            yield item

    @waitcursor
    def get_mosaics(
        self, name_contains: str | None = None
    ) -> Iterator[dict[str, dict]]:
        """
        List all available mosaics you have access to, optionally filtering
        based on name.

        :param str name_contains:
            Search only for mosaics that contain the specified substring in
            their names
        """
        for item in self._list(
            "basemaps/v1/mosaics", key="mosaics", name__contains=name_contains
        ):
            yield item

    def get_mosaics_for_series(self, series_id: str) -> dict[Any]:
        """
        Get the mosaics in a series.
        """
        endpoint = "basemaps/v1/series/{}/mosaics".format(series_id)
        url = self._url(endpoint)
        return self._get(url)

    def get_quads_for_mosaic(
        self,
        mosaic: str | dict[Any, Any],
        bbox: str | float = None,
        minimal: bool = False,
    ) -> Iterator[Any]:
        """List all available quads for a given mosaic"""
        if isinstance(mosaic, str):
            mosaicid = mosaic
        else:
            mosaicid = mosaic["id"]

        if bbox is None:
            if isinstance(mosaic, str):
                bbox = [-180, -85, 180, 85]
            else:
                bbox = mosaic["bbox"]
        bbox = (
            max(-180, bbox[0]),
            max(-84.99, bbox[1]),
            min(180, bbox[2]),
            min(84.99, bbox[3]),
        )

        bbox_str = ",".join(str(item) for item in bbox)

        endpoint = f"basemaps/v1/mosaics/{mosaicid}/quads/"

        # Previous version quads was a dictionary and now is an Iterator
        quads = self._consume_pages(endpoint, "items", bbox=bbox_str, minimal="true")
        return quads

    def get_one_quad(self, mosaic: dict[Any, Any]) -> dict[str, Any]:
        """Get a single quad for a given mosaic"""
        quads = list(self.get_quads_for_mosaic(mosaic, mosaic["bbox"]))
        quad = quads[0]
        return quad

    def get_items_for_quad(self, mosaicid: str, quadid: str):
        """Retrieve information for all quads for a given mosaic"""
        endpoint = f"basemaps/v1/mosaics/{mosaicid}/quads/{quadid}/items"
        url = self._url(endpoint)
        items = self._get(url)["items"]
        item_descriptions = []
        for item in items:
            if item["link"].startswith("https://api.planet.com"):
                response = self._get(item["link"])
                item_descriptions.append(response)

        return item_descriptions

    def create_order(self, request):
        """Place an order through the Order API"""
        endpoint = "compute/ops/orders/v2"
        headers = {"X-Planet-App": "qgis"}
        url = self._url("compute/ops/orders/v2")
        response = self._post(url, json_data=request, headers=headers)
        return response

    def _query(self, endpoint, key, json_query):
        """Post and then get for pagination."""
        url = None

        while True:
            if url is None:
                url = self._url(endpoint)
                response = self._post(url, json_query)
            else:
                response = self._get(url)

            for item in response[key]:
                yield item

            if "_next" in response["_links"]:
                url = response["_links"]["_next"]
            else:
                break

    def update_search(self, request, searchid) -> dict[Any, Any]:
        """Updates an existing saved search configuration in Planet Data API v1."""
        body = json.dumps(request)

        endpoint = f"data/v1/searches/{searchid}"
        url = self._url(endpoint)

        response = self.session.put(url, data=body, headers=headers)
        response.raise_for_status()
        return response.json()

    def delete_search(self, searchid):
        """Permanently delete a saved search filter"""

        endpoint = f"data/v1/searches/{searchid}"
        url = self._url(endpoint)

        response = self.session.delete(
            url,
        )
        response.raise_for_status()

        """
        if response.status_code == 204:
            print("Successfully deleted! Nothing returned.")
        else:
            # If it's not a 204, there might be an error message payload to read
            print(response.json())
        """
        return response.json()

    @pyqtSlot(result=bool)
    def update_user_quota(self):
        """
        Example quota response

        [
          {
            "active_from": "2019-08-01T00:00:00+00:00",
            "active_to": null,
            "basemap_quad_quota": null,
            "basemap_tile_quota": null,
            "created_at": "2019-08-01T18:33:11.551737+00:00",
            "datadrop_anchor_date": "2019-08-01T00:00:00+00:00",
            "datadrop_enabled": false,
            "datadrop_interval": null,
            "deleted_at": null,
            "id": 301722,
            "organization": {
              "id": 150098,
              "name": "Planet Federal"
            },
            "organization_id": 150098,
            "plan": {
              "id": 1262,
              "name": "Timelapse Basemaps Web Service",
              "state": "active"
            },
            "plan_id": 1262,
            "quota_anchor_date": "2019-08-01T00:00:00+00:00",
            "quota_enabled": false,
            "quota_interval": null,
            "quota_reset_at": null,
            "quota_sqkm": null,
            "quota_style": "consumption",
            "quota_used": 0.0,
            "reference": "PL-0123456",
            "selected_operations": null,
            "state": "active",
            "updated_at": "2019-08-01T18:33:11.551737+00:00",
            "url": "https://api.planet.com/auth/v1/experimental/public/"
                   "subscriptions/301722"
          },
          ...
        ]
        """
        if not self.api_key():
            log.warning("No API key found for getting quota")
            return False

        # TODO: Catch errors
        # TODO: Switch to async call
        # response = self.dispatcher.dispatch_request(
        #     method="GET", url=QUOTA_URL, auth=self.auth)

        response = self._get(QUOTA_URL)
        log.debug(f"resp_data:\n{response}")  # noqa: E231
        if not response:
            log.warning("No response data found for getting quota")
            return False

        quota_data = response[0]
        quota_keys = ["quota_enabled", "quota_sqkm", "quota_used"]
        has_quota_data = all([q in quota_data for q in quota_keys])

        if has_quota_data:
            quota_enabled = bool(quota_data["quota_enabled"])
            self._user_quota["enabled"] = quota_enabled
            self._user_quota["sqkm"] = quota_data["quota_sqkm"]
            self._user_quota["used"] = quota_data["quota_used"]
            log.debug(
                f""" Quota (sqkm)
              Enabled: {str(self.user_quota_enabled())}
              Size: {str(self.user_quota_size())}
              Used: {str(self.user_quota_used())}
              Remaining: {str(self.user_quota_remaining())}
            """
            )
        else:
            log.warning("No quota keys found in response for getting quota")
            return False

        return True

    def user_quota_enabled(self):
        return bool(self._user_quota["enabled"])

    def user_quota_size(self):
        return bool(self._user_quota["sqkm"])

    def user_quota_used(self):
        return bool(self._user_quota["used"])

    def user_quota_remaining(self):
        # if not self.update_user_quota():
        #     return None

        if self.user_quota_enabled():
            return float(self._user_quota["sqkm"]) - float(self._user_quota["used"])

        return None

    def get_mosaic_from_id(self, mosaic_id):
        info = self._item(f"basemaps/v1/mosaics/{mosaic_id}")
        return info

    def asset_types_for_item_type(self, item_type):
        if item_type not in self._asset_types:
            endpoint = f"data/v1/item-types/{item_type}/asset-types"
            url = self._url(endpoint)
            response = self._get(url)
            asset_types = response["asset_types"]
            self._asset_types[item_type] = asset_types
        return self._asset_types[item_type]

    def asset_types_for_item_type_as_dict(self, item_type):
        asset_types = self.asset_types_for_item_type(item_type)
        return {a["id"]: a for a in asset_types}

    def psscene_asset_types_for_nbands(self, nbands):
        asset_types = self.asset_types_for_item_type("PSScene")
        return [
            asset["id"]
            for asset in asset_types
            if "bands" in asset and len(asset.get("bands")) >= nbands
        ]

    def item_types(self):
        if self._item_types is None:
            url = self._url("data/v1/item-types/")
            self._item_types = self._get(url)["item_types"]
            self._item_types = [v for v in self._item_types if " " in v["display_name"]]
        return self._item_types

    def item_types_names(self):
        item_types = self.item_types()
        return {t["id"]: t["display_name"] for t in item_types}

    def bundles(self):
        url = "https://us-central1-planet-webapps-prod.cloudfunctions.net/productBundles/latest"
        if self._bundles is None:
            self._bundles = self._get(url)
        return self._bundles

    def bundles_for_item_type(self, item_type):
        bundles = self.bundles()
        bndls_per_it = {
            b["id"]: b
            for b in bundles[item_type]
            if b.get("fileType") != "NITF" and b.get("auxiliaryFiles") != "udm"
        }
        return bndls_per_it

    def bundles_for_item_type_and_permissions(self, item_type, permissions):
        bundles = self.bundles_for_item_type(item_type)

        permissions_cleaned = []
        for img_permissions in permissions:
            img_permissions_cleaned = []
            for p in img_permissions:
                match = ITEM_ASSET_DL_REGEX.match(p)
                if match is not None:
                    img_permissions_cleaned.append(match.group(1))
            permissions_cleaned.append(img_permissions_cleaned)

        allowed_bundles = {}
        for name, b in bundles.items():
            add_bundle = True
            assets = b.get("assets", [])
            for asset in assets:
                for img_permissions in permissions_cleaned:
                    if asset not in img_permissions:
                        add_bundle = False
            if add_bundle:
                allowed_bundles[name] = b

        return allowed_bundles


def tile_service_hash(item_type_ids: List[str]) -> Optional[str]:
    """
    :param item_type_ids: List of item Type:IDs
    :return: Tile service hash that can be used in tile URLs
    """

    # api_key = PlanetClient.getInstance().api_key()

    if not item_type_ids:
        log.debug("No item type:ids passed, skipping tile hash")
        return None

    item_type_ids.reverse()
    data = {"ids": ",".join(item_type_ids)}

    tile_url = TILE_SERVICE_URL.format("")

    session = PlanetClient.getInstance().session
    res = session.post(tile_url, data=data)
    if res.ok:
        res_json = res.json()
        if "name" in res_json:
            return res_json["name"]
    else:
        log.debug(
            f"Tile service hash request failed:\n"  # noqa: E231
            f"status_code: {res.status_code}\n"  # noqa: E231
            f"reason: {res.reason}"
        )

    return None


def tile_service_url(
    item_type_ids: List[str], tile_hash: Optional[str] = None, service: str = "xyz"
) -> Optional[str]:
    """
    :param item_type_ids: List of item 'Type:IDs'
    :param tile_hash: Tile service hash
    :param service: Either 'xyz' or 'wmts'
    :return: Tile service URL
    """
    api_key = PlanetClient.getInstance().api_key()

    if not tile_hash:
        if not item_type_ids:
            log.debug("No item type:ids passed, skipping tile URL")
            return None
        tile_hash = tile_service_hash(item_type_ids)

    if not tile_hash:
        log.debug("No tile URL hash passed, skipping tile URL")
        return None

    from ..pe_utils import user_agent

    url = None
    if service.lower() == "wmts":
        tile_url = TILE_SERVICE_URL.format("")
        url = f"{tile_url}/wmts/{tile_hash}?api_key={api_key}"
    elif service.lower() == "xyz":
        tile_url = TILE_SERVICE_URL.format(random.randint(0, 3))
        url = (
            f"{tile_url}/{tile_hash}/{{z}}/{{x}}/{{y}}?"
            f"api_key={api_key}"
            f"&ua={user_agent()}"
        )

    return url
