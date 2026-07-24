#ifndef WCWIDTH_H_INCLUDED
#define WCWIDTH_H_INCLUDED

#include <stddef.h>   // wchar_t
#include <stdint.h>   // uint32_t

#ifdef __cplusplus
extern "C" {
#endif

// 返回值：-1 非打印控制字符；0 零宽；1 单宽；2 双宽（中日韩等）
// 接受 wchar_t（BMP 内字符）
int wcwidth(wchar_t ucs);

// 同上，但接受 32 位 codepoint（支持 BMP 外字符如 emoji U+1F600）
// 用于代理对组合后的完整 codepoint 宽度查询
int wcwidth32(uint32_t cp);

#ifdef __cplusplus
}
#endif

#endif
