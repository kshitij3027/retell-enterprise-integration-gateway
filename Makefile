# REIG — developer convenience targets.
# Every target wraps a docker-compose invocation. Testing and local dev both
# run inside Docker per CLAUDE.md's "testing gate". Never run pytest on the
# host — always via `docker compose run --rm api pytest`.

.PHONY: prepare demo down clean logs help

help:
	@echo "REIG dev targets:"
	@echo "  make prepare  - pull base images + build local api image"
	@echo "  make demo     - bring the full stack up in the background"
	@echo "  make down     - stop the stack (keeps volumes)"
	@echo "  make clean    - stop + wipe volumes + docker prune"
	@echo "  make logs     - tail logs from the running stack"

prepare:
	docker compose pull
	docker compose build

demo:
	docker compose up -d

down:
	docker compose down

clean:
	docker compose down -v
	docker system prune -f

logs:
	docker compose logs -f --tail=100
