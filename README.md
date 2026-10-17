# Rack — a local dashboard for your home server apps

A tiny, self-contained web app that gives you one page to launch all your
self-hosted services (Jellyfin, Navidrome, and whatever else you run).

No build step, no dependencies, no backend — just three files.

## Running it

Any of these work:

**Just open the file**
Double-click `index.html`, or open it in your browser via `file://`.

**Or serve it (recommended, avoids some browser file:// quirks)**
```bash
cd homelab-gallery
python3 -m http.server 8000
```
Then visit `http://localhost:8000`.

## How it works

- **Add a service** — click "Add service", give it a name and URL.
- **Default thumbnail** — if you don't provide one, it uses the site's own
  favicon (fetched via Google's favicon service, so it always loads even if
  the target server itself can't be reached from a plain `<img>` tag).
- **Custom thumbnail** — drag an image file onto the drop zone in the
  add/edit panel (or click it to browse). You can also drag an image
  straight onto an existing card on the grid to swap its thumbnail without
  opening the edit panel.
- **Click a card** — opens that service in a new tab.
- **Edit / remove** — hover a card and click the pencil icon in the corner.

## Data

Everything (your list of apps, names, URLs, and any custom thumbnails) is
stored in your browser's `localStorage`, under the key `rack.apps.v1`.
Nothing is sent anywhere — this is a purely local, offline app. If you
switch browsers or clear site data, the list resets.
