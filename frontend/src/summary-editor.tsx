import { useLayoutEffect, useRef, useState } from "react";
import { Fragment, Schema, Slice } from "prosemirror-model";
import { EditorState, type Command } from "prosemirror-state";
import { EditorView } from "prosemirror-view";
import { baseKeymap, chainCommands, liftEmptyBlock, setBlockType, toggleMark } from "prosemirror-commands";
import { keymap } from "prosemirror-keymap";
import { history, redo, undo } from "prosemirror-history";
import { wrapInList, splitListItem, liftListItem, sinkListItem } from "prosemirror-schema-list";
import { schema as base, MarkdownParser, MarkdownSerializer, defaultMarkdownParser, defaultMarkdownSerializer } from "prosemirror-markdown";
import { summaryMarkdown } from "./summary-markup";

const schema = new Schema({
  nodes: base.spec.nodes.update("heading", { ...base.nodes.heading.spec, content: "text*" })
    .remove("blockquote").remove("code_block").remove("horizontal_rule").remove("image"),
  marks: base.spec.marks.remove("link").remove("code"),
});
const { paragraph, heading, bullet_list, ordered_list, list_item } = schema.nodes;
const parser = new MarkdownParser(schema, summaryMarkdown, {
  ...Object.fromEntries(Object.entries(defaultMarkdownParser.tokens).filter(([key]) =>
    ["paragraph", "heading", "bullet_list", "ordered_list", "list_item", "strong", "em", "hardbreak"].includes(key))),
  softbreak: { node: "hard_break" },
});
const serializer = new MarkdownSerializer(defaultMarkdownSerializer.nodes, defaultMarkdownSerializer.marks, {
  escapeExtraCharacters: /[<&]/g,
});
type Format = "plain" | "markdown";
function read(text: string, format: Format) {
  if (format === "markdown") return parser.parse(text);
  return schema.node("doc", null, text.split(/\r?\n/).map(line =>
    paragraph.create(null, line ? schema.text(line) : undefined)));
}

function listAt(state: EditorState) {
  const { $from } = state.selection;
  for (let i = $from.depth; i > 0; i--) {
    const type = $from.node(i).type;
    if (type === bullet_list || type === ordered_list) return { type, pos: $from.before(i) };
  }
}

function listCommand(type: typeof bullet_list): Command {
  return (state, dispatch) => {
    const list = listAt(state);
    if (!list) return wrapInList(type)(state, dispatch);
    if (list.type === type) return liftListItem(list_item)(state, dispatch);
    if (dispatch) dispatch(state.tr.setNodeMarkup(list.pos, type));
    return true;
  };
}

const bold = toggleMark(schema.marks.strong);
const italic = toggleMark(schema.marks.em);
const indent = sinkListItem(list_item);
const outdent = liftListItem(list_item);
const headingCommand: Command = (state, dispatch) => state.selection.$from.parent.type === heading
  ? setBlockType(paragraph)(state, dispatch) : setBlockType(heading, { level: 3 })(state, dispatch);
const keys = keymap({
  "Mod-b": bold, "Mod-i": italic, "Mod-z": undo, "Shift-Mod-z": redo, "Mod-y": redo,
  Enter: chainCommands(splitListItem(list_item), liftEmptyBlock),
  Tab: indent, "Shift-Tab": outdent,
  "Shift-Enter": (state, dispatch) => {
    if (dispatch) dispatch(state.tr.replaceSelectionWith(schema.nodes.hard_break.create()).scrollIntoView());
    return true;
  },
});

type Props = {
  value: string; format: Format; disabled: boolean; autoFocus: boolean;
  onChange: (value: string) => void;
};

export function SummaryEditor(props: Props) {
  const host = useRef<HTMLDivElement>(null);
  const view = useRef<EditorView | null>(null);
  const latest = useRef(props);
  const last = useRef({ value: props.value, format: props.format });
  const [state, setState] = useState<EditorState | null>(null);

  useLayoutEffect(() => { latest.current = props; });
  useLayoutEffect(() => {
    const initial = latest.current;
    const editor = new EditorView(host.current, {
      state: EditorState.create({ doc: read(initial.value, initial.format), plugins: [history(), keys, keymap(baseKeymap)] }),
      attributes: { role: "textbox", "aria-label": "Саммари", "aria-multiline": "true", lang: "ru", spellcheck: "true" },
      editable: () => !latest.current.disabled,
      clipboardTextParser(text, context, _plain, editor) {
        // Word and mail clients also use the literal bullet character in plain-text copies.
        const source = text.replace(/^(\s*)[•●]\s+/gm, "$1- ");
        const doc = parser.parse(source);
        const block = doc.firstChild;
        const marks = editor.state.storedMarks ?? context.marks();
        if (marks.length && doc.childCount === 1 && block?.type === paragraph && block.firstChild?.isText &&
            block.childCount === 1 && !block.firstChild.marks.length) {
          return new Slice(Fragment.from(block.firstChild.mark(marks)), 0, 0);
        }
        return Slice.maxOpen(doc.content);
      },
      dispatchTransaction(transaction) {
        const next = editor.state.apply(transaction);
        editor.updateState(next);
        setState(next);
        if (transaction.docChanged && !transaction.getMeta("fromProps")) {
          const value = serializer.serialize(next.doc);
          last.current = { value, format: "markdown" };
          latest.current.onChange(value);
        }
      },
    });
    view.current = editor;
    setState(editor.state);
    if (initial.autoFocus) editor.focus();
    return () => { editor.destroy(); view.current = null; };
  }, []);

  useLayoutEffect(() => {
    const editor = view.current;
    if (!editor) return;
    editor.setProps({ editable: () => !props.disabled });
    if (props.value === last.current.value && props.format === last.current.format) return;
    last.current = { value: props.value, format: props.format };
    const doc = read(props.value, props.format);
    if (!editor.state.doc.eq(doc)) editor.dispatch(editor.state.tr.replaceWith(0, editor.state.doc.content.size, doc.content).setMeta("fromProps", true));
  }, [props.value, props.format, props.disabled]);

  function button(name: string, label: string, command: Command, active?: boolean) {
    return <button type="button" aria-label={name} title={name} aria-pressed={active}
      disabled={!state || props.disabled || !command(state)} onMouseDown={e => e.preventDefault()} onClick={() => {
        const editor = view.current;
        if (editor) { command(editor.state, editor.dispatch); editor.focus(); }
      }}>{label}</button>;
  }
  const marks = state?.storedMarks ?? state?.selection.$from.marks() ?? [];
  function marked(mark: typeof schema.marks.strong) {
    if (!state || state.selection.empty) return Boolean(mark.isInSet(marks));
    return state.doc.rangeHasMark(state.selection.from, state.selection.to, mark);
  }
  const list = state && listAt(state);
  return <div className="rich-summary">
    <div className="summary-toolbar" role="group" aria-label="Форматирование саммари">
      {button("Полужирный", "Ж", bold, marked(schema.marks.strong))}
      {button("Курсив", "К", italic, marked(schema.marks.em))}
      {button("Подзаголовок", "Подзаголовок", headingCommand, state?.selection.$from.parent.type === heading)}
      {button("Маркированный список", "• Список", listCommand(bullet_list), list?.type === bullet_list)}
      {button("Нумерованный список", "1. Список", listCommand(ordered_list), list?.type === ordered_list)}
      {button("Уменьшить отступ", "←", outdent)}
      {button("Увеличить отступ", "→", indent)}
      {button("Отменить правку", "↶", undo)}
      {button("Повторить правку", "↷", redo)}
    </div>
    <div className="summary-edit-surface summary-content" ref={host} />
  </div>;
}
