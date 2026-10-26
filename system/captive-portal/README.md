# Captive portal design

JP-PisoWiFi v0.1 implements the captive portal natively with nftables instead of NoDogSplash:

1. Unauthorized HTTP (TCP 80) from the customer LAN is DNAT-ed to the portal on the Pi.
2. The Flask app answers any foreign `Host` (e.g. `connectivitycheck.gstatic.com`) with a redirect to
   `http://<gateway>/`, which is what makes phones show the "sign in to network" prompt.
3. Forwarding is allowed only for MACs in the kernel set `authorized` (each element carries a timeout equal to
   the remaining session time).
4. Everything else from the LAN is rejected quickly (TCP reset / ICMP unreachable).

Why not NoDogSplash first: it has to be built/installed and matched to the iptables-vs-nftables backend of the
installed Armbian release, and it was not possible to test that here. The control interface is isolated behind
`backend/network_service.py` (`authorize/revoke/list_authorized`), so NoDogSplash can be added as another backend
later without touching payments or sessions. Evaluate it on the real board if you need HTTPS-interception
behaviour or its walled-garden features.

Known captive-portal limits (true for any implementation): HTTPS sites cannot be redirected without certificate
errors, so detection relies on the phone's own HTTP probe; some devices/browsers with "private DNS" or randomised
MAC addresses behave differently, and a customer who "forgets" the network may get a new MAC and a new session.
