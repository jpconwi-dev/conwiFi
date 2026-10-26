# Network and WiFi router (AP) setup

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
