`hiddify14_admin_capabilities.py` is the Hiddify 14.2.0 panel's
`services/panel/src/hiddifypanel/admin_capabilities.py`, copied as a policy fixture.
The creation regression executes its merge/default/effective-permission functions
with only `AdminMode` stubbed, so tests do not need a panel database or running app.
It retains the upstream panel source and license terms. Do not replace it with the
invoice application's expected policy: the two independent policies must agree.
