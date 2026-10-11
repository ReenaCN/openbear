import {computed, ref, watch} from "vue";
import {Api, apiError} from "../../api.js";
import {useToolDetailCache} from "./toolDetailCache.js";

function detailText(operation) {
	const payload = operation?.payload && typeof operation.payload === "object" ? operation.payload : {};
	if (typeof payload.text === "string") return payload.text;
	return Array.isArray(payload.textChunks) ? payload.textChunks.join("") : "";
}

/**
 * Finished reasoning is delivered without its text. Fetch it once, only while
 * the card is open, through the same volatile per-conversation detail cache as
 * tool cards.
 */
export function useDeferredReasoning({event, open, conversationUuid}) {
	const cache = useToolDetailCache();
	const loadedText = ref("");
	const loadedFor = ref("");
	const loading = ref(false);
	const error = ref("");
	const operation = computed(() => event()?.operation || {});
	const opId = computed(() => String(operation.value.opId || ""));
	const deferred = computed(() => Boolean(event()?.message?.reasoningDeferred) && Boolean(opId.value));
	const inlineText = computed(() => String(event()?.message?.reasoning || ""));
	const text = computed(() => inlineText.value || (loadedFor.value === opId.value ? loadedText.value : ""));

	async function load() {
		const uuid = String(conversationUuid() || "").trim();
		const id = opId.value;
		if (!open() || !deferred.value || inlineText.value || loadedFor.value === id || loading.value) return;
		if (!uuid || uuid.startsWith("local:")) return;
		loading.value = true;
		error.value = "";
		try {
			const request = async () => {
				const data = await Api.conversationOperationDetail(uuid, id);
				if (data?.ok === false) throw new Error(data.error || "思考内容读取失败");
				return data?.operation || {};
			};
			const detail = cache
				? await cache.load({conversationUuid: uuid, operationId: id, revision: Number(operation.value.revision || 0) || 0, loader: request})
				: await request();
			if (opId.value !== id) return;
			loadedText.value = detailText(detail);
			loadedFor.value = id;
		} catch (err) {
			if (opId.value === id) error.value = apiError(err);
		} finally {
			loading.value = false;
		}
	}

	watch([open, opId, deferred], () => { load(); }, {immediate: true});
	return {text, loading, error, deferred};
}
