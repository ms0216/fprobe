// SPDX-License-Identifier: GPL-2.0-only
/*
 * fprobe_ringmap: 深いバッファのリング（デバイスツリーの reserved-memory の fprobe_ring）を、読み取り専用で
 * 利用者空間に写すだけの文字デバイス /dev/fprobe_ring。設計 docs/superpowers/specs/2026-11-01-bandwidth-observation-design.md
 * の 1.7.4 のモジュール (a)。中継のゼロコピーの道（1.7.9）が使う。起動時の読み込みは fprobe_gw pynq ringmap が置く
 * （/etc/modules-load.d。2026-11-02 に利用者が承認）。
 *
 * なぜ要るか: /dev/mem の写像は VM_PFNMAP（struct page を引けない）なので、そこから TCP の MSG_ZEROCOPY で送ると
 * get_user_pages が断り EFAULT になる。予約のページは no-map でないので struct page を持つ（PG_reserved）。それを
 * vm_insert_pages で写すと VM_MIXEDMAP の普通のページの写像になり、MSG_ZEROCOPY がページをそのまま NIC に渡せる。
 *
 * 場所はコードに書かない: /reserved-memory の子のうち名前が fprobe_ring のノード（ただ 1 つ）の reg を使う。
 * 中継（fprobe-relay の relay_region_reserved）と同じく、no-map・reusable・compatible のどれかがあれば断る
 * （Linux が触りうる、または struct page が無い）。読み込み時に全ページが pfn_valid かつ PageReserved であることを確かめ、
 * 1 つでも違えば読み込まない。
 *
 * 写像: 読み取り専用（PROT_WRITE は EPERM、VM_MAYWRITE も外す）。CPU はリングに書かない。キャッシュの属性は
 * カーネルの直線の写像と同じ（キャッシュあり）で、別の属性の別名を作らない。PL は HP（非コヒーレント）で DDR に書くので、
 * この写像を CPU が読むと古い行を読みうる。送るだけ（NIC の DMA が DDR を読む）なら CPU は中身を読まない。
 * 注意（設計 1.7.4）: カーネルがゼロコピーをやめて写した場合（完了通知の SO_EE_CODE_ZEROCOPY_COPIED）は、写しが
 * キャッシュありの直線の写像を読むので、古い中身を送りうる。使う側がその通知を数えること。
 */
#include <linux/fs.h>
#include <linux/miscdevice.h>
#include <linux/mm.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/of_address.h>
#include <linux/stringify.h>

#define RINGMAP_BATCH 64                /* vm_insert_pages に 1 回で渡すページ数（スタックに置く配列の大きさ） */

static unsigned long ring_pfn;          /* 先頭のページ番号 */
static unsigned long ring_pages;        /* ページ数 */

/* /reserved-memory の fprobe_ring を探し、その reg を *res に入れる。ただ 1 つで、Linux が触らない形のときだけ 0 */
static int ringmap_find(struct resource *res)
{
	struct device_node *rm, *np, *hit = NULL;
	int n = 0, err;

	rm = of_find_node_by_path("/reserved-memory");
	if (!rm) {
		pr_err("fprobe_ringmap: /reserved-memory not found\n");
		return -ENODEV;
	}
	for_each_child_of_node(rm, np) {
		if (!of_node_name_eq(np, "fprobe_ring"))
			continue;
		if (n++ == 0)
			hit = of_node_get(np);
	}
	of_node_put(rm);
	if (n != 1) {
		pr_err("fprobe_ringmap: expected exactly 1 fprobe_ring node, found %d\n", n);
		of_node_put(hit);
		return -ENODEV;
	}
	if (of_property_read_bool(hit, "no-map") || of_property_read_bool(hit, "reusable") ||
	    of_property_read_bool(hit, "compatible")) {
		pr_err("fprobe_ringmap: %pOF has no-map/reusable/compatible\n", hit);
		of_node_put(hit);
		return -ENODEV;
	}
	err = of_address_to_resource(hit, 0, res);
	of_node_put(hit);
	if (err) {
		pr_err("fprobe_ringmap: cannot read reg (%d)\n", err);
		return err;
	}
	if (!PAGE_ALIGNED(res->start) || !PAGE_ALIGNED(resource_size(res)) || resource_size(res) == 0) {
		pr_err("fprobe_ringmap: %pR not page aligned\n", res);
		return -EINVAL;
	}
	return 0;
}

static int ringmap_mmap(struct file *file, struct vm_area_struct *vma)
{
	unsigned long npages = vma_pages(vma), off = vma->vm_pgoff, done = 0;
	struct page *pages[RINGMAP_BATCH];

	if (off >= ring_pages || npages > ring_pages - off)
		return -EINVAL;
	if (vma->vm_flags & VM_WRITE)
		return -EPERM;
	/* 後から mprotect で書けるようにしない・広げない・コアダンプに入れない */
	vm_flags_mod(vma, VM_DONTEXPAND | VM_DONTDUMP, VM_MAYWRITE);

	while (done < npages) {
		unsigned long k, n = min_t(unsigned long, npages - done, RINGMAP_BATCH), left = n;
		int err;

		for (k = 0; k < n; k++)
			pages[k] = pfn_to_page(ring_pfn + off + done + k);
		/* VM_MIXEDMAP を立て、各ページの参照を 1 つ増やして写す。失敗の途中までの写像は呼び手（mmap_region）が外す */
		err = vm_insert_pages(vma, vma->vm_start + (done << PAGE_SHIFT), pages, &left);
		if (err)
			return err;
		done += n;
	}
	return 0;
}

static const struct file_operations ringmap_fops = {
	.owner = THIS_MODULE,               /* 開いている（写している）間は rmmod できない */
	.mmap = ringmap_mmap,
};

static struct miscdevice ringmap_dev = {
	.minor = MISC_DYNAMIC_MINOR,
	.name = "fprobe_ring",
	.fops = &ringmap_fops,
	.mode = 0400,                       /* /dev/mem と同じく root だけ（リングには対象の観測の中身が入る） */
};

/* 全ページの参照の数えの最小と最大（読み込みと外すときに記録し、写像と送信の後に元へ戻ったかを見る） */
static void ringmap_refcounts(int *lo, int *hi)
{
	unsigned long i;

	*lo = INT_MAX;
	*hi = INT_MIN;
	for (i = 0; i < ring_pages; i++) {
		int c = page_count(pfn_to_page(ring_pfn + i));

		*lo = min(*lo, c);
		*hi = max(*hi, c);
	}
}

static int __init ringmap_init(void)
{
	struct resource res;
	unsigned long i;
	int err, lo, hi;

	err = ringmap_find(&res);
	if (err)
		return err;
	ring_pfn = PHYS_PFN(res.start);
	ring_pages = resource_size(&res) >> PAGE_SHIFT;
	for (i = 0; i < ring_pages; i++) {
		unsigned long pfn = ring_pfn + i;

		if (!pfn_valid(pfn) || !PageReserved(pfn_to_page(pfn))) {
			pr_err("fprobe_ringmap: pfn 0x%lx is not a reserved page with struct page\n", pfn);
			return -ENODEV;
		}
	}
	ringmap_refcounts(&lo, &hi);
	err = misc_register(&ringmap_dev);
	if (err)
		return err;
	pr_info("fprobe_ringmap: /dev/%s maps %pR (%lu reserved pages, refcount %d..%d)\n",
		ringmap_dev.name, &res, ring_pages, lo, hi);
	return 0;
}

static void __exit ringmap_exit(void)
{
	int lo, hi;

	misc_deregister(&ringmap_dev);
	ringmap_refcounts(&lo, &hi);
	pr_info("fprobe_ringmap: unloaded (refcount %d..%d)\n", lo, hi);
}

module_init(ringmap_init);
module_exit(ringmap_exit);
MODULE_LICENSE("GPL");
MODULE_DESCRIPTION("fprobe: read-only vm_insert_pages map of the reserved deep-buffer ring");
/* 作ったソースの sha256（build.sh が make に FPROBE_RINGMAP_SRC=<16 進> で渡す）。据え付けの道具（fprobe_gw pynq ringmap）が
 * modinfo -F fprobe_src で読み、据え付けてあるモジュールがリポジトリのソースと同じかを決める（作り直すかの判断） */
#ifdef FPROBE_RINGMAP_SRC
MODULE_INFO(fprobe_src, __stringify(FPROBE_RINGMAP_SRC));
#endif
