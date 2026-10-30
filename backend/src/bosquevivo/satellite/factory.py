from bosquevivo.core.config import Settings
from bosquevivo.satellite.provider import SatelliteProvider
from bosquevivo.satellite.providers.fake import FakeProvider
from bosquevivo.satellite.providers.gee import GeeProvider
from bosquevivo.satellite.providers.hansen import HansenOpenProvider
from bosquevivo.satellite.providers.opendata import OpenDataProvider


def build_provider(settings: Settings) -> SatelliteProvider:
    if settings.satellite_provider == "fake":
        return FakeProvider()
    if settings.satellite_provider == "open":
        return OpenDataProvider()
    if settings.satellite_provider == "hansen":
        return HansenOpenProvider()
    return GeeProvider(settings.gee_project, settings.google_application_credentials)
