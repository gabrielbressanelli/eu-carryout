from datetime import datetime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo

from django.utils import timezone


class OrderingHours:
    def __init__(self, tenant, now=None):
        self.tenant = tenant
        self.zone = ZoneInfo(tenant.timezone)
        self.now = (now or timezone.now()).astimezone(self.zone)
        self.weekly = {row.day_of_week: row for row in tenant.business_hours.all()}
        self.overrides = {row.date: row for row in tenant.hours_overrides.filter(date__gte=self.now.date())}

    def window(self, date):
        if not self.tenant.is_active or self.tenant.ordering_paused:
            return None
        row = self.overrides.get(date) or self.weekly.get(date.weekday())
        if not row or row.is_closed or not row.opens_at or not row.closes_at or row.opens_at >= row.closes_at:
            return None
        return (datetime.combine(date, row.opens_at, self.zone), datetime.combine(date, row.closes_at, self.zone))

    def real_local_time(self, value):
        return value.astimezone(dt_timezone.utc).astimezone(self.zone) == value

    @property
    def is_open(self):
        window = self.window(self.now.date())
        return bool(window and window[0] <= self.now < window[1])

    @property
    def earliest(self):
        return (self.now.astimezone(dt_timezone.utc) + timedelta(minutes=self.tenant.preparation_minutes)).astimezone(self.zone)

    @property
    def asap(self):
        window = self.window(self.now.date())
        return self.earliest if self.is_open and self.earliest < window[1] else None

    def slots(self):
        if not self.tenant.scheduled_pickup_enabled:
            return []
        slots = []
        date = self.now.date()
        window = self.window(date)
        if not window:
            return slots
        start, end = window
        slot = start + timedelta(minutes=self.tenant.preparation_minutes)
        while slot < end:
            if slot >= self.earliest and self.real_local_time(slot):
                slots.append(slot)
            slot += timedelta(minutes=self.tenant.pickup_interval_minutes)
        return slots

    def validate_pickup(self, selection):
        if selection == "asap":
            if self.asap:
                return self.asap
            raise ValueError(self.message)
        try:
            value = datetime.fromisoformat(selection)
            if value.tzinfo is None:
                raise ValueError
        except (ValueError, TypeError):
            raise ValueError("Choose an available pickup time.")
        valid = next((slot for slot in self.slots() if slot == value), None)
        if valid is None:
            raise ValueError("That pickup time is no longer available. Choose another time.")
        return valid

    @property
    def message(self):
        if self.tenant.ordering_paused:
            return self.tenant.closure_message or "Online ordering is temporarily paused."
        if self.asap:
            return f"Pickup in approximately {self.tenant.preparation_minutes} minutes."
        if self.is_open:
            return "Today's pickup cutoff has passed."
        for offset in range(31):
            window = self.window(self.now.date() + timedelta(days=offset))
            if window and window[0] > self.now:
                return f"Closed now. Opens {window[0].strftime('%a, %b %d at %I:%M %p')}."
        return "Online ordering is currently unavailable."

    def payload(self):
        slots = self.slots()
        return {"is_open": self.is_open, "asap_available": bool(self.asap), "message": self.message,
                "timezone": self.tenant.timezone, "can_order": bool(self.asap or slots),
                "slots": [{"value": slot.isoformat(), "label": slot.strftime("%a, %b %d at %I:%M %p")} for slot in slots]}
