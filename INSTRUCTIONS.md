Build a lightweight local web UI for an LXC that downloads MEGA links to local storage and visually matches the soft card-based Homepage design shown in the reference screenshot.



Core requirements:

1\. Use Python 3 and Flask.

2\. Keep it lightweight and easy to update.

3\. Use server-rendered HTML/CSS, no React, no database server.

4\. Use a small modular structure rather than one giant file.

5\. Match Homepage’s visual style:

&#x20;  - pale blue background

&#x20;  - thin top divider

&#x20;  - rounded cards

&#x20;  - compact stat tiles

&#x20;  - soft shadows

&#x20;  - simple typography

6\. The app should run well inside an LXC on a LAN.



Main features:

1\. Multiple MEGA URLs can be pasted at once into a textarea.

2\. Parse one URL per line, trim whitespace, discard blanks, deduplicate.

3\. User selects a destination path from configured locations.

4\. Start downloads as queued jobs.

5\. Show live per-file and aggregate:

&#x20;  - current speed

&#x20;  - downloaded bytes

&#x20;  - total bytes when known

&#x20;  - ETA when known

6\. Built in file explorer rooted only inside allowed directories.

7\. File explorer supports:

&#x20;  - folder navigation

&#x20;  - file size display

&#x20;  - modification time

&#x20;  - unzip action for zip files

&#x20;  - password field for encrypted zip files

8\. ZIP extraction must support standard zip and password protected AES zip files.

9\. Keep security tight:

&#x20;  - never allow path traversal outside allowed roots

&#x20;  - sanitize archive extraction paths

&#x20;  - no shell injection

10\. Expose a small JSON API for polling so the UI can refresh job status every 1-2 seconds.



Architecture:

\- app.py: Flask app factory and route registration

\- models.py: dataclasses for Job, TransferStatus, ExplorerEntry

\- downloader.py: queue manager and MEGA adapter

\- archives.py: unzip logic using pyzipper and zipfile

\- explorer.py: safe file browser helpers

\- storage.py: lightweight JSON persistence

\- templates/base.html

\- templates/index.html

\- templates/explorer.html

\- static/style.css

\- static/app.js



Implementation details:

\- Use a background worker thread and an in-memory queue.

\- Persist job state to a small JSON file so jobs/history survive app restarts as much as possible.

\- Implement a DownloadManager class.

\- Each submitted URL becomes its own job object, grouped under a batch if multiple URLs were submitted together.

\- Add job statuses:

&#x20; queued, starting, probing, downloading, completed, failed, canceled

\- For live updates, use lightweight polling via fetch() every 1500 ms, not websockets.

\- Build endpoints:

&#x20; GET /                    dashboard

&#x20; POST /submit             submit one or more URLs

&#x20; GET /api/jobs            JSON job list and summary

&#x20; POST /jobs/<id>/cancel   cancel a job

&#x20; GET /explorer            browse files

&#x20; POST /unzip              extract archive

&#x20; GET /api/explorer        optional JSON explorer endpoint

\- Allowed roots should be configured in config.py as named destinations.



Downloader design:

\- Implement a MegaDownloader adapter.

\- Start with MEGAcmd via subprocess using mega-get for each public link.

\- Capture stdout/stderr.

\- Parse progress information if present.

\- If machine-readable totals are unavailable initially, show:

&#x20; speed = unknown

&#x20; eta = estimating

&#x20; total size = unknown

&#x20; until enough data is available.

\- Track:

&#x20; bytes\_done

&#x20; bytes\_total

&#x20; speed\_bps

&#x20; eta\_seconds

&#x20; started\_at

&#x20; finished\_at

\- Aggregate batch totals across all jobs in the batch.



Explorer design:

\- Only show paths under configured download roots.

\- Use pathlib.Path.resolve() and reject any path outside allowed roots.

\- Explorer cards should visually match Homepage service cards.

\- Support sorting by name, size, modified.

\- Show actions:

&#x20; open folder

&#x20; unzip

&#x20; delete later as optional, but do not implement in first pass unless already easy



ZIP extraction:

\- Use zipfile for normal zip reading.

\- Use pyzipper for AES encrypted zip extraction.

\- User can optionally supply a password.

\- Prevent zip slip by validating each extracted path remains inside target extraction directory.



Styling:

\- Reuse the previous soft Homepage-like styling as the base.

\- Add:

&#x20; - textarea for multi-link input

&#x20; - destination dropdown

&#x20; - job list cards

&#x20; - progress bars

&#x20; - explorer table/card layout

&#x20; - unzip modal or inline form



Acceptance criteria:

1\. User can paste 3 MEGA URLs and submit once.

2\. App creates 3 jobs and shows them separately plus a batch summary.

3\. User can choose destination path before submitting.

4\. Explorer shows downloaded files inside the selected root.

5\. User can unzip a zip file from the explorer.

6\. Password protected zip extraction works when the correct password is supplied.

7\. App never browses or extracts outside allowed roots.

8\. UI visually feels similar to Homepage and works well on desktop and tablet.



Do this in phases:

Phase 1:

\- scaffold files

\- build Flask app

\- Homepage-like layout

\- multi-URL parsing

\- destinations

\- in-memory queue

\- fake downloader adapter for development

Phase 2:

\- real MEGAcmd integration

\- live status polling

\- speed and ETA

Phase 3:

\- file explorer

\- unzip support

\- password protected zip support

Phase 4:

\- persistence

\- cancel/retry

\- polish

\- systemd service and README



Also generate:

\- requirements.txt

\- config.example.py

\- README.md

\- systemd unit file

