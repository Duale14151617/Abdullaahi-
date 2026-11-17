from django.core.management.base import BaseCommand
from django.db import transaction

from apps.detention import facilities


class Command(BaseCommand):
    help = ("Create a jail center (police lock-up) for every police station and one generic prison per region. "
            "Safe to run again: it only adds what is missing.")

    def handle(self, *args, **opts):
        with transaction.atomic():
            jails, prisons = facilities.ensure_defaults()
        self.stdout.write(self.style.SUCCESS(f"Custody facilities ready: {jails} jail centers and {prisons} prisons added."))
