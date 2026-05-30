import { createContext, useContext } from "react";
import { PaletteMode } from "@mui/material";

export const ColorModeContext = createContext<{ mode: PaletteMode; toggle: () => void }>({
  mode: "light",
  toggle: () => {},
});

export const useColorMode = () => useContext(ColorModeContext);
