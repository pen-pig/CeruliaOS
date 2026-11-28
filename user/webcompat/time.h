#pragma once
typedef long long time_t;
struct tm {long tm_gmtoff;};
struct timespec {time_t tv_sec;long tv_nsec;};
#define CLOCK_REALTIME 0
int clock_gettime(int,struct timespec *);
struct tm *localtime_r(const time_t *,struct tm *);
