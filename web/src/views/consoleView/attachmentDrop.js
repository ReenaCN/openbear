// Only native file drags belong to attachments. Text, links and OpenBear
// references must keep their existing editor/tree drag-and-drop behavior.
export function hasDraggedFiles(transfer) {
	return Boolean(transfer && (
		Array.from(transfer.types || []).includes("Files") ||
		Array.from(transfer.items || []).some(item => item.kind === "file") ||
		transfer.files?.length
	));
}

export function createAttachmentDropHandlers({onFiles, onWarning, setActive, blockedReason}) {
	let depth = 0;
	function reset() {
		depth = 0;
		setActive(false);
	}
	function accept(event) {
		if (!hasDraggedFiles(event.dataTransfer)) return false;
		// Capture before the rich-text editor consumes the drop or the browser
		// opens the file, replacing the current conversation page.
		event.preventDefault();
		event.stopPropagation();
		return true;
	}
	function dragEnter(event) {
		if (!accept(event)) return;
		depth++;
		setActive(true);
	}
	function dragOver(event) {
		if (!accept(event)) return;
		event.dataTransfer.dropEffect = blockedReason() ? "none" : "copy";
		setActive(true);
	}
	function dragLeave(event) {
		if (!hasDraggedFiles(event.dataTransfer) && !depth) return;
		depth = Math.max(0, depth - 1);
		if (!depth) reset();
	}
	function drop(event) {
		reset();
		if (!accept(event)) return;
		const reason = blockedReason();
		if (reason) { onWarning(reason); return; }
		const transfer = event.dataTransfer;
		const items = Array.from(transfer.items || []).filter(item => item.kind === "file");
		let directories = false;
		// Read the DataTransfer synchronously: browsers protect it again as
		// soon as the drop handler returns. Reuse File objects, not their bytes.
		const files = items.length ? items.flatMap(item => {
			if (item.webkitGetAsEntry?.()?.isDirectory) { directories = true; return []; }
			const file = item.getAsFile?.();
			return file ? [file] : [];
		}) : Array.from(transfer.files || []);
		if (directories) onWarning("暂不支持拖入文件夹，请选择其中的文件");
		if (files.length) onFiles(files);
		else if (!directories) onWarning("未能读取拖入的文件，请使用附件按钮选择文件");
	}
	return {dragEnter, dragOver, dragLeave, drop, reset};
}
