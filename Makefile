# REIG — developer convenience targets.
# Every target wraps a docker-compose invocation. Testing and local dev both
# run inside Docker per CLAUDE.md's "testing gate". Never run pytest on the
# host — always via `docker compose run --rm api pytest`.

.PHONY: prepare demo down clean logs help seed fire-webhook fire-inbound test

help:
	@echo "REIG dev targets:"
	@echo "  make prepare      - pull base images + build local api image"
	@echo "  make demo         - bring the full stack up in the background"
	@echo "  make seed         - create the two demo tenants + issue API keys"
	@echo "  make test         - run the full pytest suite inside the api container"
	@echo "  make fire-webhook - POST a signed call_analyzed webhook (SC-1/2/3)"
	@echo "                      vars: PAYLOAD= TENANT= [TIMES=N] [TAMPER=1]"
	@echo "  make fire-inbound - POST a signed call_inbound hydration webhook (SC-8)"
	@echo "                      vars: TENANT= PHONE=+14155551234"
	@echo "  make logs         - tail logs from the running stack"
	@echo "  make down         - stop the stack (keeps volumes)"
	@echo "  make clean        - stop + wipe volumes + docker prune"

prepare:
	docker compose pull
	docker compose build

demo:
	docker compose up -d

seed:
	docker compose run --rm api python -m scripts.seed

test:
	docker compose run --rm api pytest -q

# Example:
#   make fire-webhook PAYLOAD=tests/fixtures/valid_call_analyzed.json TENANT=<uuid>
#   make fire-webhook PAYLOAD=tests/fixtures/valid_call_analyzed_with_pii.json \
#                     TENANT=<uuid> TIMES=5
#   make fire-webhook PAYLOAD=tests/fixtures/valid_call_analyzed.json TENANT=<uuid> TAMPER=1
fire-webhook:
	@[ -n "$(PAYLOAD)" ] || (echo "usage: make fire-webhook PAYLOAD=... TENANT=..."; exit 2)
	@[ -n "$(TENANT)" ] || (echo "usage: make fire-webhook PAYLOAD=... TENANT=..."; exit 2)
	docker compose exec -T api bash scripts/fire_webhook.sh \
		--payload $(PAYLOAD) \
		--tenant $(TENANT) \
		$(if $(TIMES),--times $(TIMES),) \
		$(if $(TAMPER),--tamper,)

# Example:
#   make fire-inbound TENANT=<uuid> PHONE=+14155551234
fire-inbound:
	@[ -n "$(TENANT)" ] || (echo "usage: make fire-inbound TENANT=... PHONE=+E164"; exit 2)
	@[ -n "$(PHONE)" ]  || (echo "usage: make fire-inbound TENANT=... PHONE=+E164"; exit 2)
	docker compose exec -T api bash scripts/fire_inbound_hydration.sh \
		--tenant $(TENANT) \
		--phone $(PHONE)

down:
	docker compose down

clean:
	docker compose down -v
	docker system prune -f

logs:
	docker compose logs -f --tail=100
