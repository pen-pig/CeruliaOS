/* Local accounts and the single active console/desktop session. */
#include "user.h"
#include "fs.h"
#include "task.h"

#define ACCOUNT_LIMIT 16
#define ACCOUNT_V1 0x31414343u
#define ACCOUNT_V2 0x32414343u
#define ACCOUNT_ADMIN 1
#define ACCOUNT_PASSWORD 2

typedef struct {
    char name[USER_NAME_MAX + 1];
    uint8_t salt[AUTH_SALT], hash[AUTH_DIGEST];
} account_v1_t;

typedef struct {
    account_v1_t key;
    uint8_t flags;
} account_t;

typedef struct {
    uint32_t magic, count, sequence;
    account_v1_t account[ACCOUNT_LIMIT];
    uint8_t checksum[AUTH_DIGEST];
} account_db_v1_t;

typedef struct {
    uint32_t magic, count, sequence;
    account_t account[ACCOUNT_LIMIT];
    uint8_t checksum[AUTH_DIGEST];
} account_db_t;

_Static_assert(sizeof(account_db_v1_t) == 1084, "CCA1 disk format");
_Static_assert(sizeof(account_db_t) == 1100, "CCA2 disk format");

static account_db_t accounts, pending;
static account_db_v1_t legacy;
static uint32_t boot_mount, cur_uid;
static int loaded, broken, active, storage_pinned;
static char cur_home[USER_HOME_SIZE] = "/";

static int u_eq(const char *a, const char *b) {
    while (*a && *b) { if (*a != *b) return 0; a++; b++; }
    return *a == *b;
}

static void u_copy(char *out, const char *s) {
    while ((*out++ = *s++)) {}
}

int user_valid_account_name(const char *name) {
    if (!name || !*name) return 0;
    unsigned len = 0;
    for (; *name; name++) {
        char c = *name;
        if (++len > USER_NAME_MAX ||
            !((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) return 0;
    }
    return 1;
}

static void make_home(char home[USER_HOME_SIZE], const char *name) {
    u_copy(home, "/home/");
    u_copy(home + 6, name);
}

int user_uid_of(const char *name) {
    if (!user_valid_account_name(name)) return -1;
    for (uint32_t i = 0; i < accounts.count; i++)
        if (u_eq(name, accounts.account[i].key.name)) return (int)i;
    return -1;
}

const char *user_name_of(uint32_t uid) {
    return uid < accounts.count ? accounts.account[uid].key.name : 0;
}

int user_account_admin(uint32_t uid) {
    return uid < accounts.count && (accounts.account[uid].flags & ACCOUNT_ADMIN);
}

int user_password_required(const char *name) {
    int id = user_uid_of(name);
    return id < 0 || (accounts.account[id].flags & ACCOUNT_PASSWORD);
}

static int checksum_valid(const void *data, uint32_t size, const uint8_t *checksum) {
    uint8_t digest[AUTH_DIGEST];
    auth_hash(data, size, digest);
    return auth_equal(digest, checksum, sizeof(digest));
}

static int validate_accounts(void) {
    if (accounts.magic != ACCOUNT_V2 || accounts.count == 0 || accounts.count > ACCOUNT_LIMIT)
        return 0;
    for (uint32_t i = 0; i < accounts.count; i++) {
        const account_t *a = &accounts.account[i];
        const char *name = a->key.name;
        if (name[USER_NAME_MAX] || !user_valid_account_name(name) ||
            (a->flags & ~(ACCOUNT_ADMIN | ACCOUNT_PASSWORD)) ||
            ((a->flags & ACCOUNT_PASSWORD) && !accounts.sequence)) return 0;
        for (uint32_t j = 0; j < i; j++)
            if (u_eq(name, accounts.account[j].key.name)) return 0;
    }
    return user_account_admin(0) && u_eq(accounts.account[0].key.name, "root");
}

static int read_accounts(uint32_t size) {
    if (size == sizeof(accounts)) {
        return fs_read_file(FS_ACCOUNT_DB, (uint8_t *)&accounts, size) == (int)size &&
               checksum_valid(&accounts, __builtin_offsetof(account_db_t, checksum), accounts.checksum) &&
               validate_accounts();
    }
    if (size != sizeof(legacy) ||
        fs_read_file(FS_ACCOUNT_DB, (uint8_t *)&legacy, size) != (int)size ||
        legacy.magic != ACCOUNT_V1 || legacy.count == 0 || legacy.count > ACCOUNT_LIMIT ||
        !checksum_valid(&legacy, __builtin_offsetof(account_db_v1_t, checksum), legacy.checksum)) return 0;
    accounts.magic = ACCOUNT_V2;
    accounts.count = legacy.count;
    accounts.sequence = legacy.sequence;
    for (uint32_t i = 0; i < legacy.count; i++) {
        accounts.account[i].key = legacy.account[i];
        accounts.account[i].flags = ACCOUNT_PASSWORD | (i == 0 ? ACCOUNT_ADMIN : 0);
    }
    auth_wipe(&legacy, sizeof(legacy));
    return validate_accounts();
}

void user_load_accounts(void) {
    if (loaded) return;
    boot_mount = fs_mount_generation();
    if (fs_get_info()->type == FS_EXFAT) {
        uint32_t size = 0, marker_size;
        uint8_t dir = 0, marker_dir;
        int has_db = fs_stat(FS_ACCOUNT_DB, &size, &dir) == 0;
        if (has_db || fs_stat(FS_ACCOUNT_MARKER, &marker_size, &marker_dir) == 0) {
            storage_pinned = 1;
            if (!has_db || dir || !read_accounts(size)) {
                broken = 1;
                auth_wipe(&accounts, sizeof(accounts));
            }
            loaded = 1;
            return;
        }
    }
    accounts.magic = ACCOUNT_V2;
    accounts.count = 2;
    u_copy(accounts.account[0].key.name, "root");
    accounts.account[0].flags = ACCOUNT_ADMIN;
    u_copy(accounts.account[1].key.name, "user");
    loaded = 1;
}

int user_managed(void) { return loaded; }
int user_accounts_broken(void) { return broken; }
int user_signed_in(void) { return !loaded || active; }
int user_is_admin(void) { return !loaded || (active && user_account_admin(cur_uid)); }
int user_confined(void) { return loaded && active && !user_is_admin(); }
int user_storage_pinned(void) { return storage_pinned; }
int user_can_store(void) { return fs_get_info()->type == FS_EXFAT && fs_mount_generation() == boot_mount; }
const char *user_home(void) { return cur_home; }
uint32_t user_current_uid(void) { return cur_uid; }
uint32_t user_current_gid(void) { return cur_uid; }
const char *user_current_name(void) {
    const char *name = user_name_of(cur_uid);
    return name ? name : "root";
}

int user_session_busy(void) {
    uint32_t n;
    const task_t *tasks = task_table(&n);
    for (uint32_t i = 0; i < n; i++) {
        if (tasks[i].is_user && tasks[i].state != TASK_UNUSED && tasks[i].state != TASK_ZOMBIE)
            return 1;
    }
    return 0;
}

static int password_length_ok(uint32_t len) {
    return len >= AUTH_MIN_PASSWORD && len <= AUTH_MAX_PASSWORD;
}

static int set_password(account_t *entry, const char *password, uint32_t len) {
    if (!password_length_ok(len) || pending.sequence == 0xffffffffu) return -1;
    auth_salt(entry->key.salt, ++pending.sequence);
    auth_password(password, len, entry->key.salt, entry->key.hash);
    entry->flags |= ACCOUNT_PASSWORD;
    return 0;
}

static int save_pending(int volatile_ok) {
    if (!user_can_store()) {
        if (storage_pinned || !volatile_ok) return -1;
        accounts = pending;
        return USER_VOLATILE;
    }
    auth_hash(&pending, __builtin_offsetof(account_db_t, checksum), pending.checksum);
    if (fs_save_account_db(&pending, sizeof(pending)) != 0) return -1;
    accounts = pending;
    storage_pinned = 1;
    return 0;
}

static int verify_account(int id, const char *password, uint32_t len) {
    if (id >= 0 && !(accounts.account[id].flags & ACCOUNT_PASSWORD)) return len == 0;
    /* Unknown names still do the expensive check. */
    const account_v1_t *entry = &accounts.account[id < 0 ? 0 : id].key;
    uint8_t hash[AUTH_DIGEST];
    if (len > AUTH_MAX_PASSWORD) return 0;
    auth_password(password, len, entry->salt, hash);
    int ok = auth_equal(hash, entry->hash, sizeof(hash));
    auth_wipe(hash, sizeof(hash));
    return ok && id >= 0;
}

int user_auth_login(const char *name, const char *password, uint32_t len) {
    if (!loaded || broken || user_session_busy()) return -1;
    int id = user_uid_of(name);
    if (!verify_account(id, password, len)) return -1;
    make_home(cur_home, accounts.account[id].key.name);
    /* A missing/read-only data disk must not stop console or desktop boot. */
    fs_enter_account_home(cur_home, (uint32_t)id);
    cur_uid = (uint32_t)id;
    active = 1;
    return 0;
}

int user_logout(void) {
    if (!active || user_session_busy()) return -1;
    active = 0;
    cur_uid = 0;
    u_copy(cur_home, "/");
    return 0;
}

int user_add_account(const char *name, const char *password, uint32_t len) {
    if (!active || !user_is_admin() || user_session_busy() || !user_valid_account_name(name) ||
        !password_length_ok(len) || accounts.count >= ACCOUNT_LIMIT) return -1;
    if (user_uid_of(name) >= 0) return -2;
    /* Don't promise a password-protected account exists after restart without durable storage. */
    if (!user_can_store()) return -3;
    char home[USER_HOME_SIZE];
    make_home(home, name);
    if (fs_prepare_account_home(home, accounts.count, 0) != 0) return -3;
    pending = accounts;
    account_t *entry = &pending.account[pending.count++];
    u_copy(entry->key.name, name);
    if (set_password(entry, password, len) == 0 && save_pending(0) == 0) return 0;
    fs_rmdir(home);
    return -3;
}

int user_set_admin(const char *name, int enable) {
    if (!active || !user_is_admin() || user_session_busy()) return -1;
    int id = user_uid_of(name);
    if (id < 0) return -2;
    if (id == 0 && !enable) return -3;
    if (!!user_account_admin((uint32_t)id) == !!enable) return 0;
    pending = accounts;
    if (enable) pending.account[id].flags |= ACCOUNT_ADMIN;
    else pending.account[id].flags &= (uint8_t)~ACCOUNT_ADMIN;
    int rc = save_pending(1);
    if (rc < 0) return -4;
    if (!enable && (uint32_t)id == cur_uid) fs_enter_account_home(cur_home, cur_uid);
    return rc;
}

int user_check_password(const char *password, uint32_t len) {
    return active && verify_account((int)cur_uid, password, len);
}

int user_change_password(const char *password, uint32_t len) {
    if (!active || user_session_busy() || !user_can_store()) return -1;
    pending = accounts;
    if (set_password(&pending.account[cur_uid], password, len) != 0) return -1;
    return save_pending(0);
}
