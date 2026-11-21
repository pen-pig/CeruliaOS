/*
 * Local console and desktop accounts, enabled on every boot.
 */
#ifndef USER_H
#define USER_H

#include "types.h"
#include "auth.h"

#define USER_NAME_MAX 16
#define USER_HOME_SIZE (6 + USER_NAME_MAX + 1)
#define USER_VOLATILE 1

void user_load_accounts(void);
int user_managed(void);
int user_accounts_broken(void);
int user_signed_in(void);
int user_is_admin(void);
int user_account_admin(uint32_t uid);
int user_password_required(const char *name);
int user_storage_pinned(void);
int user_can_store(void);
int user_confined(void);
const char *user_home(void);
int user_valid_account_name(const char *name);
int user_session_busy(void);
int user_auth_login(const char *name, const char *password, uint32_t len);
int user_logout(void);
int user_add_account(const char *name, const char *password, uint32_t len);
int user_set_admin(const char *name, int enable);
int user_check_password(const char *password, uint32_t len);
int user_change_password(const char *password, uint32_t len);

uint32_t    user_current_uid(void);
uint32_t    user_current_gid(void);
const char *user_current_name(void);

int user_uid_of(const char *name);
const char *user_name_of(uint32_t uid);

#endif
