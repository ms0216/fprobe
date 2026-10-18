/* freestanding の最小の string.h（FreeRTOS が使う関数だけ。実体は libc/string.c）。 */
#ifndef FREERTOS_DEMO_STRING_H
#define FREERTOS_DEMO_STRING_H
#include <stddef.h>
void *memcpy(void *dst, const void *src, size_t n);
void *memset(void *dst, int value, size_t n);
size_t strlen(const char *s);
char *strcpy(char *dst, const char *src);
#endif
