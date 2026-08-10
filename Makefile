DATA_DIR   := data/Zomboid
SERVER_DIR := server
BACKUP_DIR := backups

.PHONY: wipe-world wipe-mods fix-perms

restart:
	docker compose restart zomboid

wipe-world:
	@printf 'Delete the world and every player record? [y/N] ' && read ans && [ "$$ans" = "y" ]
	docker compose down
	sudo rm -rf $(DATA_DIR)/Saves $(DATA_DIR)/db $(DATA_DIR)/backups
	@echo 'Done. A fresh world is generated on the next: docker compose up -d'

wipe-mods:
	@printf 'Delete all downloaded Workshop mods? [y/N] ' && read ans && [ "$$ans" = "y" ]
	docker compose down
	sudo rm -rf $(SERVER_DIR)/steamapps/workshop
	@echo 'Done. The next start re-downloads them; expect a long boot.'

fix-perms:
	mkdir -p $(BACKUP_DIR)
	sudo chown -R $$(id -un):$$(id -gn) $(DATA_DIR) $(SERVER_DIR) $(BACKUP_DIR)
	sudo setfacl -R -m  u:$$(id -un):rwX $(DATA_DIR) $(SERVER_DIR) $(BACKUP_DIR)
	sudo setfacl -R -d -m u:$$(id -un):rwX $(DATA_DIR) $(SERVER_DIR) $(BACKUP_DIR)