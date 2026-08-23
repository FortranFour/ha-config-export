# Making the folder buttons clickable

The Daily / Weekly / Monthly / Yearly buttons on the export card point at `file://` URLs.
**Browsers block `file://` links opened from an http(s) page**, so out of the box those
buttons do nothing. One policy re-enables them.

> This whitelists *all* `file://` links, not just these. Any page you visit can then offer
> clickable links into your local filesystem. Clicking is still required, but it is a real
> loosening of a browser default — decide knowingly. If you would rather not, the card prints
> the path as selectable text; paste it into your file manager.

## Windows (Edge / Chrome)

Elevated Command Prompt:

```
reg add "HKLM\SOFTWARE\Policies\Microsoft\Edge\URLAllowlist" /v 1 /t REG_SZ /d "file:///" /f
reg add "HKLM\SOFTWARE\Policies\Google\Chrome\URLAllowlist" /v 1 /t REG_SZ /d "file:///" /f
```

Fully quit and reopen the browser, then confirm at `edge://policy` or `chrome://policy`. To
undo, swap `add` for `delete` and drop the `/t /d` arguments.

Prefer a drive letter? Map the share to `Z:` and use `file:///Z:/daily/` in the card.

## macOS

```
defaults write com.google.Chrome URLAllowlist -array "file:///"
defaults write com.microsoft.Edge URLAllowlist -array "file:///"
```

Then point the four `url_path` lines at the mount:
`file:///Volumes/share/ha_config_backup/daily/`

## Linux

```
sudo mkdir -p /etc/opt/chrome/policies/managed
echo '{"URLAllowlist": ["file:///"]}' | sudo tee /etc/opt/chrome/policies/managed/allow_file_links.json
```

For Edge use `/etc/opt/edge/policies/managed/`. Then use
`file:///mnt/ha-share/ha_config_backup/daily/`.

## The companion app

`file://` cannot be opened from the Home Assistant companion app under any configuration.
This is desktop-browser only.
