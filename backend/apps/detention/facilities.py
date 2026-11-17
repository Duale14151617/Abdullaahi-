"""Default custody facilities: one jail center (police lock-up) for every police station, and one generic prison per region.

Jail center = the station's own cells, short-term, run by the Police. Prison = long-term, run by the prison authority, outside the
Police; here it is only a place a person is handed over to. The generic names are placeholders to rename in the Custody facilities page.
Idempotent: it only creates what is missing and never touches a name an administrator has edited.
"""
from apps.organization import police_structure as ps
from apps.organization.models import OrganizationalUnit

from .models import CustodyFacility

PRISON_AUTHORITY = "Prison authority (not the Police)"


def ensure_jail_center(station):
    """The jail center of one police station. Returns (facility, created)."""
    code = f"{station.code}-JAIL"
    fac = CustodyFacility.objects.filter(code=code).first()
    if fac is not None:
        return fac, False
    return CustodyFacility.objects.create(
        code=code, kind="jail_center", org_unit=station, operated_by="Police",
        name_en=f"Jail center: {station.name_en}", name_so=f"Xarunta Jeelka: {station.name_so or station.name_en}"), True


def ensure_defaults():
    """Returns (jail centers made, prisons made)."""
    jails = prisons = 0
    for st in OrganizationalUnit.objects.filter(unit_type__code="POLICE_STATION", active=True):
        jails += ensure_jail_center(st)[1]
    for rcode, name in ps.REGIONS:
        code = f"PRISON-{rcode}"
        if not CustodyFacility.objects.filter(code=code).exists():
            CustodyFacility.objects.create(code=code, kind="prison", name_en=f"{name} Prison", name_so=f"Xabsiga {name}", operated_by=PRISON_AUTHORITY)
            prisons += 1
    return jails, prisons
