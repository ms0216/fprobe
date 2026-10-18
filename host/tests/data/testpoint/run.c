/* テストポイントの挿入の試験: ホストで実行して、記録した ID の列が実行の順と合うかを見る */
int depth;
int marks[3];

int fact(int n) {
    if (n <= 1) return 1;
    return n * fact(n - 1);
}

int classify(int v) {
    switch (v) {
    case 0: return 10;
    case 1:
    case 2: depth++;   /* fallthrough */
    case 3: return 20;
    default: break;
    }
    return 30;
}

int find(const int *a, int n, int key) {
    for (int i = 0; i < n; i++) {
        if (a[i] == key) return i;
    }
    return -1;
}

int cleanup(int x) {
    if (x < 0) goto bad;
    marks[x % 3] = x;
    return x;
bad:
    return 0;
}

void touch(int v) {
    while (v > 0) v--;
    depth = v;
}
