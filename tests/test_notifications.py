from mongoose.services.notifications import NotificationIn, route_notification


def test_private_notification_not_spoken():
    n = NotificationIn(source="test", app="mail", title="Private", body="secret", privacy="high", urgency="normal")
    route = route_notification(n)
    assert route["routed_to"] == "mac_dashboard"
    assert route["speak_allowed"] is False


def test_low_urgency_household_can_route_to_room_surface():
    n = NotificationIn(source="test", app="door", title="Door", body="open", privacy="low", urgency="normal")
    route = route_notification(n)
    assert route["routed_to"] == "room_tablet"
    assert route["speak_allowed"] is True
