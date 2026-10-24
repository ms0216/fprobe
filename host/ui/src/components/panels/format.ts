// パネルの値の表し方
export const hex32 = (n: number) => `0x${(n >>> 0).toString(16).toUpperCase().padStart(8, "0")}`;

