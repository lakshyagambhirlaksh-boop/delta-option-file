# Delta Options Strategy

Local Streamlit app for a scheduled short OTM strangle on Delta Exchange India.

## Strategy implemented

- Underlyings: BTC, ETH, XAUT.
- For each underlying, manually choose an expiry.
- ATM is the listed strike nearest to the option-chain spot price.
- Sell the call two strike steps above ATM and sell the put two steps below ATM.
- Choose equal contract quantity for both legs for each underlying.
- Entry orders are IOC limit sells at the current best bid. The app checks that displayed bid size covers the requested quantity on both legs.
- After both legs fill, the combined premium is the sum of the two actual average fill prices. Stop loss is 2× this credit; take profit is 0.5×.
- The app monitors current best asks as the combined buyback cost, and sends reduce-only market buys for both legs when a threshold is reached.
- Daily IST windows: 21:30–05:30 and 09:30–15:30. It makes one entry attempt per instrument per window, only during the first 90 seconds after the window opens. It closes open pairs at the window end. It does not re-enter during the same window.

## Run

Install dependencies and configure `.env`:

```powershell
python -m pip install -r requirements.txt
notepad .env
```

Then open **two PowerShell windows** in this folder:

```powershell
python -m streamlit run app.py --server.address 127.0.0.1
```

```powershell
python worker.py
```

Put `DELTA_API_KEY` and `DELTA_API_SECRET` in `.env`. After saving them, start/restart both processes so they load the keys. Never share these values in chat or commit `.env`. Use an API key with read and trading permissions, and set Delta's IP allowlist where available.

Select expiry and size for all three underlyings, review the displayed legs/depth, then tick the live authorization and arm the schedule. Keep the worker window, computer, and internet running during the trading windows. The Streamlit page can be closed while the worker continues. Disarming prevents new entries but the worker continues monitoring existing positions.

## Risks and operational limits

- The pair's combined stop and target are worker-managed; Delta does not receive a native combined-premium bracket order. A worker/process/network/API interruption can delay exits.
- Short call losses can be very large; short options also require sufficient margin. The stated stop is a trigger rule, not a guaranteed loss cap.
- Exit orders are reduce-only market orders. Liquidity, price gaps, API failure, or a halted market can affect the fill.
- The app checks best bid/ask size at the top of book, not full order-book depth. It skips entry if displayed top bid size is below the chosen quantity.
- It assumes the app is the only system trading these selected option contracts; do not manually change these positions while the schedule is managing them.
- One attempt per instrument per time window is persisted in SQLite (`trading_state.db`). Review strategy run states in the app and verify fills/positions in Delta.

Delta API documentation: https://docs.delta.exchange/

## Deploy on a VPS with Docker

This runs the Streamlit dashboard and the strategy worker as separate services with a shared persistent data volume. Use an always-on VPS with Docker Engine and the Docker Compose plugin installed. The dashboard port is bound to the VPS itself, not exposed publicly.

1. Copy the project files to the VPS, then create the environment file:

	```sh
	cp .env.example .env
	nano .env
	chmod 600 .env
	```

	Set `DELTA_API_KEY` and `DELTA_API_SECRET` in `.env`. Do not commit or share this file. If Delta uses an IP allowlist, allow the VPS's outbound IP.

2. Build and start both services:

	```sh
	docker compose up -d --build
	docker compose ps
	docker compose logs -f worker
	```

3. Open the dashboard from your computer through an SSH tunnel:

	```sh
	ssh -L 8501:127.0.0.1:8501 USER@VPS_IP
	```

	Then visit `http://127.0.0.1:8501` locally. To provide a public URL, put a reverse proxy with HTTPS and authentication in front of Streamlit; do not expose the trading controls directly to the internet.

The Docker volume `strategy_data` keeps the SQLite database and worker lock across container rebuilds. Back up that volume before changing or removing it. Check `docker compose logs -f worker` and confirm the dashboard reports the worker online before considering the strategy operational. Live orders remain stopped until explicitly authorized and started in the dashboard.

To stop the services without deleting persisted strategy state:

```sh
docker compose down
```
