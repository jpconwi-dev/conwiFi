# Network and WiFi setup

Two ways to provide the customer WiFi. Pick one:

## Option A (recommended for 1 USB port): the Pi is the access point (USB WiFi adapter, e.g. Alfa AWUS036ACM)
```
Source router --(LAN port)--> Pi built-in Ethernet = WAN
Pi USB WiFi adapter (hostapd) = customer LAN 10.0.0.1/24 --> customers' phones
```
- Needs an adapter whose driver supports AP mode. The AWUS036ACM (MediaTek MT7612U, driver `mt76x2u`) reports `AP` in `iw list` on Armbian trixie.
- Install with `sudo bash installer/install.sh --wifi-ap` (add `--wan <built-in> --lan <wlx...>` if auto-detection is ambiguous).
- SSID, open/password, band, channel and maximum devices are changed in the admin panel (Network page). Saving restarts the AP; active sessions are kept and
  phones reconnect by themselves. The WiFi name in Branding is the same setting.
- Default is an **open** network (customers pay through the portal). Open means traffic is not encrypted over the air, like any open hotspot; set a password
  if you prefer, at the cost of customers needing it first.
- One radio = one band at a time. Use 2.4 GHz channel 1, 6 or 11 for the widest phone support; 5 GHz only offers non-DFS channels 36-48.
- `ap_isolate=1` is set so customers cannot see each other's devices; the firewall also never forwards LAN to LAN.
- The Orange Pi One has one USB host port and USB 2.0 speeds. Use a good 5 V 2 A+ supply; a powered hub is better if you add more USB devices.
- Not verified on hardware yet: hostapd behaviour with your adapter, concurrent-client limits and range. Test with the checklist.

## Option B: external WiFi router in AP/bridge mode + USB Ethernet adapter

```
Internet-source router --(LAN port)--> Orange Pi built-in Ethernet = WAN
Orange Pi USB Ethernet = LAN (10.0.0.1/24) --> LAN port of the customer WiFi router (AP/bridge mode)
```
## Configure the WiFi router as an access point
1. Log in to the router (reset it first if you do not know its settings).
2. Set operation mode to *Access Point* / *Bridge* if available; otherwise disable NAT/routing, then:
3. Turn **DHCP server OFF**.
4. Give the router a static management IP such as `10.0.0.2`, netmask `255.255.255.0`, gateway `10.0.0.1`.
5. Turn **client/AP isolation OFF**.
6. Plug the Orange Pi's USB Ethernet into one of the router's **LAN** ports (never its WAN/Internet port).
7. Set SSID/password/channel on the router; these are not managed by JP-PisoWiFi.

If the router has no AP/bridge mode and cannot disable NAT, every customer appears as one device and per-customer control,
accounting and speed limits will not work. Options: another AP, or flash OpenWrt if your model is supported.

## Choosing interfaces
`sudo jp-pisowifi detect` lists Ethernet interfaces (built-in vs USB). The installer auto-selects only if there is exactly one of each.
Otherwise pass `--wan` and `--lan`. Names like `enx00e04c...` are stable per adapter MAC.

## Safety
- The installer refuses if your SSH session runs over the LAN interface.
- `sudo jp-pisowifi firewall-restore` removes JP-PisoWiFi's firewall table.
- Network changes made in the admin panel auto-roll back after `network.confirm_seconds` unless confirmed from the new address.
- WAN settings are never modified. Existing WAN DHCP/NetworkManager config is left alone; NetworkManager is only told to ignore the LAN adapter (asks first).
- If the upstream router also uses 10.0.0.0/24, change the customer LAN in `config.json` before installing (e.g. 10.77.0.1).

## Admin access
The panel listens on port 80 with the portal but returns 404 to customer-side devices. Reach it from the upstream network at `http://<Pi WAN IP>/admin`
(find the IP on your source router), or allow-list a device MAC (System page or `sudo jp-pisowifi admin-allow-mac aa:bb:...`).
SSH is only allowed from the WAN side.
