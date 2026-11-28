#pragma once
static inline int isdigit(int c){return c>='0'&&c<='9';}
static inline int isspace(int c){return c==' '||(c>=9&&c<=13);}
static inline int isalpha(int c){return (c>='A'&&c<='Z')||(c>='a'&&c<='z');}
static inline int tolower(int c){return c>='A'&&c<='Z'?c+32:c;}
static inline int isxdigit(int c){return isdigit(c)||(tolower(c)>='a'&&tolower(c)<='f');}
